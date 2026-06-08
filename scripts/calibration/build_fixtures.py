"""Build Week 2 calibration fixtures.

Generates two deliberately-pathological PDBs in tmp/calibration/:

  1. ``oversized_<id>_<chain>.pdb`` — a single-chain target around 440 aa.
     Used to probe per-tool hard caps on tools.ranomics.com. Should
     trigger OOM or near-OOM on at least 1-2 of the 4 binder tools.

  2. ``gappy_<id>_<chain>_<delete_range>.pdb`` — a small chain with a
     deliberate internal gap (one stretch of residues physically removed
     from the deposited model). Used to confirm RFdiffusion's contig
     builder actually asserts on internal gaps (the v1 rule
     ``needs_fix_on_any_gap=True`` is a hypothesis to verify with this).

Both fixtures are derived from public RCSB PDBs so the geometry is real
(not synthetic colinear backbones that would create degenerate frames
unrelated to size / gaps).

Run:
    python scripts/calibration/build_fixtures.py

Idempotent — re-runs skip download and re-emit the gappy variant from
the cached source.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tmp" / "calibration"


# Picked because chain A is ~440 aa standard amino acids — sits 10% above
# the tools-hub v1 hard_cap=400 for rfantibody / rfdiffusion / boltzgen
# (and 25% above bindcraft's 350). Tubulin αβ heterodimer; chain A is
# α-tubulin from bovine brain. Known clean structure, single-conformation.
OVERSIZED_SOURCE = "1JFF"
OVERSIZED_CHAIN = "A"

# Picked because chain A is ~150 aa standard amino acids — small enough
# to clear MIN_TARGET_RESIDUES=30 after we delete a stretch, large
# enough that the remaining residues span both sides of the gap.
# T4 lysozyme; well-validated, no internal disorder in the deposited model.
GAPPY_SOURCE = "2LZM"
GAPPY_CHAIN = "A"
# Delete residues 60..69 (10-residue internal gap, mid-chain) so the
# scan_chain_gaps detector emits a length=10 internal gap and RFdiffusion's
# contig builder is forced to declare a range that includes the missing
# residues.
GAPPY_DELETE_START = 60
GAPPY_DELETE_END = 69


def _fetch(pdb_id: str) -> Path:
    """Download a PDB from RCSB. Cached locally on second call."""
    out = FIXTURES_DIR / f"{pdb_id.lower()}_raw.pdb"
    if out.exists():
        print(f"[fetch] cached {out.name}")
        return out
    url = f"https://files.rcsb.org/download/{pdb_id.upper()}.pdb"
    print(f"[fetch] {url}")
    urllib.request.urlretrieve(url, out)
    return out


def _filter_chain(raw_path: Path, chain: str) -> list[str]:
    """Return the ATOM/HETATM/TER lines for one chain, plus any HEADER /
    TITLE / DBREF / SEQRES / HELIX / SHEET / END / CRYST records.

    Stripping non-target chains keeps the calibration fixture single-chain
    and matches what tools-hub passes into the pipeline normalizer.
    """
    out: list[str] = []
    for line in raw_path.read_text(encoding="utf-8", errors="replace").splitlines():
        rec = line[:6]
        if rec in ("HEADER", "TITLE ", "CRYST1", "END   ", "END"):
            out.append(line)
            continue
        if rec.startswith("DBREF"):
            # Only keep DBREF lines for the target chain (col 13).
            if len(line) > 12 and line[12] == chain:
                out.append(line)
            continue
        if rec in ("SEQRES", "HELIX ", "SHEET "):
            # SEQRES has chain id in col 12; HELIX/SHEET in cols 20+ but
            # filtering is best-effort. Skip these — pipeline doesn't read
            # them; CRYST + ATOM is enough.
            continue
        if rec.startswith("ATOM") or rec.startswith("HETATM") or rec.startswith("TER"):
            if len(line) > 21 and line[21] == chain:
                out.append(line)
            continue
    if not any(l.startswith("END") for l in out):
        out.append("END")
    return out


def _delete_residues(
    lines: list[str], chain: str, delete_start: int, delete_end: int,
) -> list[str]:
    """Drop ATOM/HETATM records whose chain+resnum falls in [start, end]."""
    keep: list[str] = []
    for line in lines:
        if (line.startswith("ATOM") or line.startswith("HETATM")) and len(line) > 26:
            if line[21] != chain:
                keep.append(line)
                continue
            try:
                resnum = int(line[22:26])
            except ValueError:
                keep.append(line)
                continue
            if delete_start <= resnum <= delete_end:
                continue
        keep.append(line)
    return keep


def _count_aa_residues(lines: list[str], chain: str) -> int:
    """Unique (resnum, icode) pairs on chain with at least a CA atom."""
    pairs = set()
    for line in lines:
        if not line.startswith("ATOM"):
            continue
        if len(line) < 26 or line[21] != chain:
            continue
        if line[12:16].strip() != "CA":
            continue
        try:
            resnum = int(line[22:26])
        except ValueError:
            continue
        icode = line[26] if len(line) > 26 else " "
        pairs.add((resnum, icode))
    return len(pairs)


def build_oversized() -> Path:
    src = _fetch(OVERSIZED_SOURCE)
    lines = _filter_chain(src, OVERSIZED_CHAIN)
    n = _count_aa_residues(lines, OVERSIZED_CHAIN)
    out = FIXTURES_DIR / f"oversized_{OVERSIZED_SOURCE.lower()}_{OVERSIZED_CHAIN}.pdb"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[oversized] {out.name} — chain {OVERSIZED_CHAIN}, {n} CA residues")
    return out


def build_gappy() -> Path:
    src = _fetch(GAPPY_SOURCE)
    lines = _filter_chain(src, GAPPY_CHAIN)
    n_before = _count_aa_residues(lines, GAPPY_CHAIN)
    lines = _delete_residues(lines, GAPPY_CHAIN, GAPPY_DELETE_START, GAPPY_DELETE_END)
    n_after = _count_aa_residues(lines, GAPPY_CHAIN)
    out = FIXTURES_DIR / (
        f"gappy_{GAPPY_SOURCE.lower()}_{GAPPY_CHAIN}_del"
        f"{GAPPY_DELETE_START}-{GAPPY_DELETE_END}.pdb"
    )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"[gappy]     {out.name} — chain {GAPPY_CHAIN}, "
        f"{n_after} CA residues (was {n_before}, "
        f"deleted {GAPPY_DELETE_START}-{GAPPY_DELETE_END})"
    )
    return out


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        build_oversized()
        build_gappy()
    except Exception as exc:
        print(f"[build_fixtures] FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"\nFixtures in {FIXTURES_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
