"""Re-derive the per-fixture, suite and mutation figures the #477 PR body and
`tools/rfd4/numbering.py` quote, with one command instead of trusting prose.

    python scripts/measure_rfd4_numbering_figures.py

Every figure it prints is measured at run time; none is typed in. It does NOT
print every figure those two quote, and the three it misses each carry their own
command instead:

* the 14 and 50 duplicate label keys, which only exist against the pre-rebuild
  blob `a4108d6d` (command in `test_each_ligand_copy_gets_its_own_asym`);
* 1HEW's 103 waters / 3 NAG split between its two author chains, which is read
  off the fixture (said so at the figure, in `pdb_to_cif`'s docstring);
* the scope doc section 10A atom counts for option B (TRP 10, ALA and GLY 1),
  which name `hotspot_atoms_for_residue` at the figure.

Exists because round 5 found the PR body's test count stale and its
`label_asym_id` prose describing a design two rounds old: a figure that lives
only in prose is unguarded.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.proteina.run_pipeline import pdb_ca_residues  # noqa: E402
from tools.rfd4.numbering import pdb_to_cif  # noqa: E402

FIXTURES = [
    REPO / "static" / "example" / "1HEW.pdb",
    REPO / "static" / "example" / "3ave_igg1_fc_dimer.pdb",
    REPO / "tests" / "fixtures" / "rfd4" / "1igt_fv_trimmed.pdb",
]
TESTS = REPO / "tests" / "test_rfd4_numbering.py"


def _atom_site_rows(cif: str) -> tuple[list[str], list[list[str]]]:
    cols: list[str] = []
    rows: list[list[str]] = []
    for line in cif.splitlines():
        s = line.strip()
        if s.startswith("_atom_site."):
            cols.append(s.split(".", 1)[1])
        elif cols and s.startswith(("ATOM", "HETATM")):
            rows.append(s.split())
        elif cols and rows:
            break
    return cols, rows


def per_fixture() -> None:
    for path in FIXTURES:
        cif, mapping = pdb_to_cif(path.read_text(errors="replace"))
        cols, rows = _atom_site_rows(cif)
        ix = {c: i for i, c in enumerate(cols)}
        polymer = [e for e in mapping.entries if e.is_polymer]
        nonpolymer = [e for e in mapping.entries if not e.is_polymer]
        author_chains = sorted({e.author.chain for e in mapping.entries})
        polymer_chains = sorted({e.author.chain for e in polymer})
        # Non-polymer residues carrying a chain id that also has polymer
        # residues -- the case the PR body's asym prose is about.
        on_polymer_chain = [e for e in nonpolymer if e.author.chain in polymer_chains]
        asyms = {r[ix["label_asym_id"]] for r in rows}
        print(f"\n{path.relative_to(REPO).as_posix()}")
        print(f"  atom_site rows                      {len(rows)}")
        print(f"  author chains                       {len(author_chains)} {author_chains}")
        print(f"  residues (mapping entries)          {len(mapping.entries)}")
        print(f"  polymer residues                    {len(polymer)}")
        print(f"  non-polymer residues                {len(nonpolymer)}")
        print(f"  non-polymer on a polymer chain id   {len(on_polymer_chain)}")
        print(f"  distinct label_asym_id in the CIF   {len(asyms)}")
        print(f"  entities                            {len({e.label_entity_id for e in mapping.entries})}")
        ref, unparsable = pdb_ca_residues(path)
        ours = {(e.author.chain, e.author.seq, e.author.icode) for e in polymer}
        print(f"  pdb_ca_residues residues            {len(ref)} (unparsable {unparsable})")
        print(f"  agrees with pdb_ca_residues         {ours == set(ref)}")
        for chain in polymer_chains:
            seqs = [e.label_seq_id for e in polymer if e.author.chain == chain]
            authors = [e.author.seq for e in polymer if e.author.chain == chain]
            print(
                f"  chain {chain}: label {min(seqs)}..{max(seqs)}"
                f"  author {min(authors)}..{max(authors)}"
            )


def _pytest(*args: str) -> str:
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS), "-q", *args],
        cwd=REPO, capture_output=True, text=True,
    ).stdout


def suite() -> None:
    print("\nsuite")
    collected = _pytest("--collect-only").strip().splitlines()[-1]
    print(f"  collected                           {collected}")
    print(f"  run                                 {_pytest().strip().splitlines()[-1]}")


def identity_mutation() -> None:
    """How many tests fail with the production mapping replaced by identity.

    The figure the PR body quotes. Measured by monkeypatching the two lookup
    methods for one pytest process. The subprocess keeps the patch out of the
    CALLING process; it is NOT an ordering requirement. These are class
    attributes, so code that imported the module earlier sees the replacement
    anyway -- measured by importing both the module and the test module first
    and patching afterwards in one process, which gave the same 18 failed, 34
    passed.
    """
    patch = (
        "import tools.rfd4.numbering as n\n"
        "n.ResidueMapping.label_for = lambda self, chain, seq, icode='': (chain, int(seq))\n"
        "n.ResidueMapping.author_for = lambda self, asym, seq_id: n.AuthorResidue(asym, int(seq_id), '')\n"
    )
    plugin = REPO / "_identity_mutation_plugin.py"
    plugin.write_text(patch)
    try:
        def run(*extra: str) -> str:
            out = subprocess.run(
                [sys.executable, "-m", "pytest", str(TESTS), "-q",
                 "-p", "_identity_mutation_plugin", *extra],
                cwd=REPO, capture_output=True, text=True,
            ).stdout
            return out.strip().splitlines()[-1]

        print("\nidentity-mapping mutation (label_seq_id := author resSeq)")
        print(f"  whole suite                         {run()}")
        easy = "the_easy_case_coincides or the_easy_fixture_cannot_witness"
        print(f"  the two easy-fixture tests          {run('-k', easy)}")
    finally:
        plugin.unlink(missing_ok=True)


if __name__ == "__main__":
    per_fixture()
    suite()
    identity_mutation()
