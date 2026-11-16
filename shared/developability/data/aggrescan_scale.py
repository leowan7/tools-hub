"""Aggrescan a3v intrinsic aggregation propensity scale.

Reference: Conchillo-Sole et al. BMC Bioinformatics 2007;8:65.
Per-residue aggregation propensity values from the Aggrescan algorithm.
Positive values indicate aggregation-promoting residues; negative indicates
aggregation-suppressing. These are the published per-residue a3v constants.
"""

AGGRESCAN_A3V: dict = {
    "I": 1.822,
    "F": 1.754,
    "V": 1.594,
    "L": 1.380,
    "Y": 1.159,
    "W": 1.037,
    "M": 0.910,
    "C": 0.604,
    "A": -0.036,
    "T": -0.159,
    "G": -0.535,
    "S": -0.294,
    "P": -0.334,
    "H": -1.033,
    "Q": -1.231,
    "N": -1.302,
    "R": -1.240,
    "K": -1.412,
    "D": -1.836,
    "E": -1.412,
}
