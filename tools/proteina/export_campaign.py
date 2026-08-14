"""Freeze a finished campaign into a self-contained, checkable export.

WHY THIS EXISTS. Two things a campaign produces are reachable only by parsing
the delivered coordinates, and neither survives in ``manifest.csv``:

  * THE SEQUENCES. proteina emits no ``sequence`` field, so
    ``/jobs/<id>/export.fasta`` returns 0 bytes and the designs look
    un-orderable. They are not — the residue names are in the PDBs the
    campaign already paid for, and this reads them back out.
  * THE PROVENANCE. job ids, call ids, per-shard runtime, the target and its
    checksum, the hotspots and the exact job spec all live in the ledger and
    the raw shard results, in three different shapes. One JSON here.

DELIBERATELY READ-ONLY over the campaign directory. It only ever writes under
``<campaign>/export/``. Re-running it is safe and idempotent, which matters
because the natural time to run it is "before we start the next tier", when
the previous tier is the thing you cannot afford to damage.

EVERY INTEGRITY FAILURE IS FATAL, not a warning. The point of the artefact is
that its absence of complaint means something: a missing PDB, a sequence whose
length contradicts the manifest, or a residue name outside the standard twenty
all stop the export rather than land a quietly wrong FASTA. "I don't want to
lose anything" is not served by a file that looks complete.

    python -m tools.proteina.export_campaign --campaign C:/path/to/tier1
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

# The standard twenty. A residue outside this set is fatal rather than "X":
# proteina designs canonical protein, so a non-standard name means the parse
# is wrong or the file is not what we think it is, and silently writing X
# would bake that into an orderable FASTA.
AA3 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V",
}
BINDER_CHAIN = "C"
SCORE_COLUMNS = ("total_reward", "af2_iptm", "af2_plddt", "binder_scrmsd",
                 "cluster_id", "rf3_score")
REFOLD_CUT_A = 5.0
IPTM_CUT = 0.80
FASTA_WRAP = 60

# A design counts as a SELF-COPY when its longest verbatim shared stretch with
# the target covers at least this much of the binder.
#
# Not a cosmetic annotation. In tier 1, 42.8% of designs reproduced a
# contiguous run of the target's own sequence instead of designing anything,
# and the rate scaled with the length budget (23% at 50-59 aa, 75% at 90-100)
# because the copied region is the ~107 aa CH3 domain and a longer budget can
# afford to fit it. Every one of them failed: zero self-copies among the
# designs that passed, median target overlap 4.2% among passes against 75%
# among refold failures.
#
# Recording it per design is what keeps the length result honest. Uncorrected,
# refold rate collapses with length (p = 2.5e-18); with self-copies excluded
# the same test is flat (p = 0.115). The length signal WAS this artefact, and
# a campaign that only kept the summary would have concluded the opposite.
SELF_COPY_FRACTION = 0.50


class ExportError(RuntimeError):
    """An integrity check failed. Never downgraded to a warning."""


def parse_binder(pdb: Path) -> tuple[str, int]:
    """One-letter sequence of the binder chain, plus its residue count.

    Keyed on ``(resseq, icode)`` rather than a running counter so an insertion
    code cannot collapse two residues into one, and first-altloc-wins so a
    disordered side chain cannot duplicate one. Both are silent
    off-by-one-per-design bugs in the sequence otherwise.
    """
    seen: dict[tuple[str, str], str] = {}
    order: list[tuple[str, str]] = []
    for line in pdb.read_text(encoding="utf-8").splitlines():
        if not line.startswith("ATOM") or len(line) < 27:
            continue
        if line[21] != BINDER_CHAIN or line[12:16].strip() != "CA":
            continue
        # NO altloc filter. Every altloc of one residue shares its
        # (resseq, icode), so first-wins below already collapses them, and
        # filtering to `A`/blank on top of that would DROP a residue modelled
        # only as altloc B — shortening the sequence for no gain.
        key = (line[22:26].strip(), line[26])
        if key in seen:
            continue
        res = line[17:20].strip().upper()
        if res not in AA3:
            raise ExportError(
                f"{pdb}: residue {res} at {key} is not one of the standard "
                "twenty. Refusing to write it as X.")
        seen[key] = AA3[res]
        order.append(key)
    return "".join(seen[k] for k in order), len(order)


def longest_common_substring(a: str, b: str) -> int:
    """Longest VERBATIM shared stretch. Deliberately not an alignment: the
    mode being detected is literal regurgitation of the input, and a gapped
    aligner would also score genuine remote similarity, which is not the same
    thing and is not disqualifying."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def target_sequences(target: Path) -> dict[str, str]:
    """Every chain in the target, one-letter. Unknown residues become X here
    rather than raising: the target is an experimental structure and may
    legitimately carry modified residues or ligands, unlike a design."""
    seen: dict[str, dict] = {}
    order: dict[str, list] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.startswith("ATOM") or len(line) < 27:
            continue
        if line[12:16].strip() != "CA":
            continue
        ch = line[21]
        key = (line[22:26].strip(), line[26])   # first altloc wins, see above
        if key in seen.setdefault(ch, {}):
            continue
        seen[ch][key] = AA3.get(line[17:20].strip().upper(), "X")
        order.setdefault(ch, []).append(key)
    return {ch: "".join(seen[ch][k] for k in order[ch]) for ch in order}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fasta_record(header: str, seq: str) -> str:
    body = "\n".join(seq[i:i + FASTA_WRAP]
                     for i in range(0, len(seq), FASTA_WRAP))
    return f">{header}\n{body}\n"


def load_ledger(campaign: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    path = campaign / "ledger.jsonl"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue          # a torn final line, same rule as ledger_replay
        if isinstance(rec.get("index"), int):
            out.setdefault(rec["index"], {}).update(rec)
    return out


def collect(campaign: Path,
            target_seqs: dict[str, str] | None = None
            ) -> tuple[list[dict], list[str]]:
    """One row per design, with its sequence. Returns (rows, notes)."""
    manifest = campaign / "manifest.csv"
    if not manifest.exists():
        raise ExportError(f"no manifest.csv under {campaign}")
    rows, notes = [], []
    with manifest.open(encoding="utf-8", newline="") as fh:
        src = list(csv.DictReader(fh))
    if not src:
        raise ExportError("manifest.csv has no rows")

    for r in src:
        rel = r.get("pdb_file", "")
        pdb = campaign / rel
        if not pdb.exists():
            raise ExportError(
                f"manifest names {rel!r} but the file is not on disk. The "
                "coordinates are the only copy of the sequence — refusing to "
                "export a partial set.")
        seq, n = parse_binder(pdb)
        if not seq:
            raise ExportError(f"{rel}: no chain {BINDER_CHAIN} CA atoms")

        # The manifest's own length column is an independent measurement of
        # the same thing. Disagreement means one of them is wrong and we do
        # not get to guess which.
        claimed = r.get("binder_length", "")
        try:
            claimed_n = int(float(claimed))
        except (TypeError, ValueError):
            claimed_n = None
            notes.append(f"{rel}: manifest binder_length {claimed!r} "
                         "unparseable; used the coordinate count")
        if claimed_n is not None and claimed_n != n:
            raise ExportError(
                f"{rel}: manifest says {claimed_n} residues, coordinates "
                f"have {n}.")

        def fnum(key):
            try:
                return float(r[key])
            except (KeyError, TypeError, ValueError):
                return None

        scrmsd, iptm = fnum("binder_scrmsd"), fnum("af2_iptm")
        refolded = scrmsd is not None and scrmsd < REFOLD_CUT_A
        row = {
            **{k: r.get(k, "") for k in (
                "shard_index", "round", "bin_lo", "bin_hi", "job_id",
                "call_id", "rank", "name", "pdb_file", *SCORE_COLUMNS)},
            "binder_length": n,
            "sequence": seq,
            "refolded": int(refolded),
            "passes": int(refolded and iptm is not None and iptm >= IPTM_CUT),
        }
        if target_seqs:
            overlap = max(longest_common_substring(seq, t)
                          for t in target_seqs.values())
            row["target_overlap"] = round(overlap / n, 4)
            row["self_copy"] = int(row["target_overlap"] >= SELF_COPY_FRACTION)
        rows.append(row)
    return rows, notes


def design_id(row: dict) -> str:
    return (f"tier1_s{int(row['shard_index']):03d}"
            f"_{row['bin_lo']}-{row['bin_hi']}"
            f"_{Path(row['pdb_file']).stem}")


def write_export(campaign: Path, dest: Path,
                 target: Path | None = None) -> dict:
    tseqs = target_sequences(target) if target else None
    if target and not tseqs:
        raise ExportError(f"{target}: no CA atoms, cannot be the target")
    rows, notes = collect(campaign, tseqs)
    dest.mkdir(parents=True, exist_ok=True)

    fields = list(rows[0].keys())
    with (dest / "designs.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    for name, subset in (("all_designs.fasta", rows),
                         ("passing.fasta", [r for r in rows if r["passes"]])):
        with (dest / name).open("w", encoding="utf-8") as fh:
            for r in subset:
                fh.write(fasta_record(
                    f"{design_id(r)} len={r['binder_length']} "
                    f"iptm={r['af2_iptm']} plddt={r['af2_plddt']} "
                    f"scrmsd={r['binder_scrmsd']} bin={r['bin_lo']}-{r['bin_hi']}",
                    r["sequence"]))

    dupes = [s for s, c in Counter(r["sequence"] for r in rows).items() if c > 1]
    ledger = load_ledger(campaign)
    prov = {
        "campaign_dir": str(campaign),
        "designs": len(rows),
        "passing": sum(r["passes"] for r in rows),
        "refolded": sum(r["refolded"] for r in rows),
        "distinct_sequences": len({r["sequence"] for r in rows}),
        "duplicate_sequences": len(dupes),
        "filters": {"refold_scrmsd_lt_A": REFOLD_CUT_A, "iptm_gte": IPTM_CUT},
        "target": None if not target else {
            "path": str(target),
            "sha256": sha256(target),
            "chains": {ch: len(s) for ch, s in tseqs.items()},
            "sequences": tseqs,
            "self_copy_fraction": SELF_COPY_FRACTION,
            "self_copies": sum(r.get("self_copy", 0) for r in rows),
            "self_copies_among_passing": sum(
                r.get("self_copy", 0) for r in rows if r["passes"]),
        },
        "gpu_seconds": sum(float(v["runtime_seconds"]) for v in ledger.values()
                           if v.get("runtime_seconds")),
        "shards": [
            {k: v.get(k) for k in ("index", "round", "bin", "job_id",
                                   "call_id", "state", "runtime_seconds",
                                   "designs", "with_atoms", "length_mismatch")}
            for _, v in sorted(ledger.items())],
        "notes": notes,
    }
    (dest / "provenance.json").write_text(
        json.dumps(prov, indent=2), encoding="utf-8")

    # A human README as well as provenance.json. The filters are the part
    # nobody remembers six months on, and "passing.fasta" does not say on its
    # face what it passed.
    t = prov["target"]
    (dest / "README.md").write_text(f"""# Campaign export

Generated by `tools/proteina/export_campaign.py` from `{campaign}`.
Re-running it reproduces this directory; it never writes outside `export/`.

## Files

| file | what it is |
|---|---|
| `designs.csv` | one row per design: scores, bin, provenance ids, **sequence** |
| `all_designs.fasta` | every design, {prov['designs']} records |
| `passing.fasta` | only designs meeting both filters, {prov['passing']} records |
| `provenance.json` | per-shard job/call ids, runtimes, target checksum |
| `CHECKSUMS.sha256` | over the source PDBs *and* these exports |

## Filters

`passes` = refolded **and** ipTM >= {IPTM_CUT}, where refolded means
`binder_scrmsd < {REFOLD_CUT_A}` A. scRMSD is AF2 self-consistency: AF2 is
given the designed sequence alone and its prediction is compared to the
designed pose.

## Numbers

- {prov['designs']} designs, {prov['refolded']} refolded, {prov['passing']} pass both
- {prov['distinct_sequences']} distinct sequences ({prov['duplicate_sequences']} appear more than once)
- {prov['gpu_seconds']/3600:.2f} GPU-hours across {len(prov['shards'])} shards
{'' if not t else f'''
## Self-copies

{t['self_copies']} designs ({100 * t['self_copies'] / prov['designs']:.1f}%) reproduce a verbatim run of the
target covering >= {100 * t['self_copy_fraction']:.0f}% of the binder — the model returning the target's
own sequence instead of designing one. **{t['self_copies_among_passing']} of them are among the passes**,
so the filters exclude the mode entirely, but the compute was still spent.
Read `target_overlap` / `self_copy` in `designs.csv` before drawing any
conclusion about binder length: the rate scales with the length budget, and
uncorrected it masquerades as a length effect.
'''}
## Sequences

proteina emits no `sequence` field, so `/jobs/<id>/export.fasta` returns 0
bytes. These sequences were read back out of the delivered coordinates, which
are the only copy. Do not delete the `shard_*/` directories.
""", encoding="utf-8")

    # LAST, so it covers every file this run wrote. Over the coordinates too,
    # not just the exports: they are the only copy of everything the sequences
    # were derived from, and an export that can only vouch for its own
    # derivatives is not much of a guarantee.
    lines = []
    for r in rows:
        p = campaign / r["pdb_file"]
        lines.append(f"{sha256(p)}  {r['pdb_file'].replace(chr(92), '/')}")
    for name in ("designs.csv", "all_designs.fasta", "passing.fasta",
                 "provenance.json", "README.md"):
        lines.append(f"{sha256(dest / name)}  export/{name}")
    (dest / "CHECKSUMS.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return prov


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--campaign", required=True, type=Path,
                    help="campaign directory (the one holding manifest.csv)")
    ap.add_argument("--dest", type=Path, default=None,
                    help="export directory (default <campaign>/export)")
    ap.add_argument("--target", type=Path, default=None,
                    help="target PDB. Adds target_overlap/self_copy columns — "
                         "the check for designs that reproduce the target's "
                         "own sequence instead of designing one.")
    args = ap.parse_args(argv)
    dest = args.dest or (args.campaign / "export")
    try:
        prov = write_export(args.campaign, dest, args.target)
    except ExportError as exc:
        print(f"[export] REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"[export] {prov['designs']} designs -> {dest}")
    print(f"         {prov['distinct_sequences']} distinct sequences, "
          f"{prov['duplicate_sequences']} duplicated")
    print(f"         {prov['refolded']} refolded, {prov['passing']} pass both "
          "filters")
    print(f"         {prov['gpu_seconds']/3600:.2f} GPU-hours across "
          f"{len(prov['shards'])} shards")
    if prov["target"]:
        t = prov["target"]
        print(f"         {t['self_copies']} self-copies "
              f"({100*t['self_copies']/prov['designs']:.1f}% of designs), "
              f"{t['self_copies_among_passing']} of them among the passes")
    for n in prov["notes"]:
        print(f"         note: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
