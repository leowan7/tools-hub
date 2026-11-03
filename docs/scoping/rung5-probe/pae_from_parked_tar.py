"""The $0 check for section 7C.3: does a parked boltz2 tar hold the PAE matrix?

Section 7C.3 left one thing UNVERIFIED - whether the released ``boltz2_conf.ckpt``
sets ``alpha_pae > 0``, which is what decides whether ``prediction["pae"]`` is
populated and the ``pae_<id>_model_<n>.npz`` written. That cannot be answered by
reading the CLI; it can be answered by existence, from output we already park on
the ``ranomics-boltz2-raw`` Volume. No fold, no GPU second.

This also computes the interface PAE the upstream ``ppi`` criterion wants
(``oracle_ppi.min_ipae_coi``, section 7A.2) from that matrix, and checks whether
the global ``iptm`` we store is or is not the same quantity as the
``pair_chains_iptm`` entry we discard.

Chain boundaries are read from the run's own ``processed/structures/*.npz``
(``chains.res_idx`` / ``chains.res_num``), never guessed from a length, so the
block split cannot silently slide. The shape assertions below fail closed.

Run against a tar fetched with ``modal volume get ranomics-boltz2-raw <name>``.
The tar is untrusted downloaded data: keep it in its own empty directory, pass
its path as an argument, and run this with ``python -I``.

    python -I docs/scoping/rung5-probe/pae_from_parked_tar.py <tar> <out.json>
"""

import io
import json
import re
import sys
import tarfile
from pathlib import Path

import numpy as np


def _load_npz(tar: tarfile.TarFile, name: str):
    return np.load(io.BytesIO(tar.extractfile(name).read()), allow_pickle=True)


def main(tar_path: Path, out_path: Path) -> None:
    tar = tarfile.open(tar_path)
    names = tar.getnames()

    pae_members = sorted(n for n in names if re.search(r"/pae_.*_model_\d+\.npz$", n))
    report = {
        "tar": tar_path.name,
        "n_members": len(names),
        "n_pae_npz": len(pae_members),
        "n_pde_npz": sum(1 for n in names if re.search(r"/pde_.*\.npz$", n)),
        "n_plddt_npz": sum(1 for n in names if re.search(r"/plddt_.*\.npz$", n)),
        "designs": [],
    }
    assert pae_members, "no pae_*.npz in this tar - alpha_pae may be 0 in the ckpt"

    for member in pae_members:
        pred_dir = member.rsplit("/", 1)[0]
        record = re.match(r"pae_(.+)_model_\d+\.npz$", Path(member).name).group(1)
        run_root = pred_dir.split("/predictions/")[0]

        pae = _load_npz(tar, member)["pae"]
        assert pae.ndim == 2 and pae.shape[0] == pae.shape[1], f"{record}: {pae.shape}"

        struct = _load_npz(tar, f"{run_root}/processed/structures/{record}.npz")
        chains = struct["chains"]
        assert len(chains) == 2, f"{record}: expected 2 chains, got {len(chains)}"
        # Token order follows chain order for protein-only inputs, so the two
        # residue blocks are contiguous and the split is chain 0's res_num.
        lens = [int(c["res_num"]) for c in chains]
        assert sum(lens) == pae.shape[0], (
            f"{record}: chains {lens} sum to {sum(lens)}, PAE is {pae.shape[0]}")
        cut = lens[0]

        # The interface blocks: binder rows x target columns and its transpose.
        # PAE is asymmetric, so both off-diagonal blocks are taken together.
        inter = np.concatenate([pae[:cut, cut:].ravel(), pae[cut:, :cut].ravel()])

        conf = json.loads(
            tar.extractfile(f"{pred_dir}/confidence_{record}_model_0.json").read())
        pair = conf.get("pair_chains_iptm") or {}
        pair_flat = {f"{a}->{b}": v for a, outer in pair.items()
                     for b, v in outer.items()}

        report["designs"].append({
            "record": record,
            "pae_dtype": str(pae.dtype),
            "pae_shape": list(pae.shape),
            "chain_names": [str(c["name"]) for c in chains],
            "chain_res_num": lens,
            "pae_min": round(float(pae.min()), 4),
            "pae_max": round(float(pae.max()), 4),
            "i_pae_mean": round(float(inter.mean()), 4),
            "i_pae_min": round(float(inter.min()), 4),
            "iptm": conf.get("iptm"),
            "pair_chains_iptm": pair_flat,
            # The question this answers: for a TWO-chain complex, is the global
            # iptm already the binder-target chain-pair number, or something else?
            "iptm_equals_pair_1_0": conf.get("iptm") == pair.get("1", {}).get("0"),
            "pair_matrix_symmetric": (
                pair.get("1", {}).get("0") == pair.get("0", {}).get("1")),
        })

    out_path.write_text(json.dumps(report, indent=2))
    for d in report["designs"]:
        print(f"{d['record']:<24} i_pAE mean {d['i_pae_mean']:>7} A   "
              f"iptm {d['iptm']}   ==pair[1][0] {d['iptm_equals_pair_1_0']}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
