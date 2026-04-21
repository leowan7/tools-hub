"""S. cerevisiae codon usage frequencies.

Reference: Kazusa codon usage database, S. cerevisiae (taxonomy id 4932),
compiled from a 14,411,073-codon protein coding gene set.
See http://www.kazusa.or.jp/codon/cgi-bin/showcodon.cgi?species=4932

Values are reported as frequency per 1000 codons. For any amino acid, the
sum of its codons' frequencies equals the amino acid's overall usage.
Rare codons (frequency < 10 per 1000) tend to be under-represented in the
yeast tRNA pool and can depress display levels of variants encoded by them.

These values are used in library_planner.codon_bias to flag positions where
a chosen degenerate scheme will over-sample codons that yeast decodes poorly.
"""

# Per-codon frequency per 1000 codons, S. cerevisiae (Kazusa 4932).
SCER_CODON_FREQ_PER_1000: dict = {
    # Phe
    "TTT": 26.1, "TTC": 18.4,
    # Leu
    "TTA": 26.2, "TTG": 27.2, "CTT": 12.3, "CTC": 5.4, "CTA": 13.4, "CTG": 10.5,
    # Ile
    "ATT": 30.1, "ATC": 17.2, "ATA": 17.8,
    # Met
    "ATG": 20.9,
    # Val
    "GTT": 22.1, "GTC": 11.8, "GTA": 11.8, "GTG": 10.8,
    # Ser
    "TCT": 23.5, "TCC": 14.2, "TCA": 18.7, "TCG": 8.6, "AGT": 14.2, "AGC": 9.8,
    # Pro
    "CCT": 13.5, "CCC": 6.8, "CCA": 18.3, "CCG": 5.3,
    # Thr
    "ACT": 20.3, "ACC": 12.7, "ACA": 17.8, "ACG": 8.0,
    # Ala
    "GCT": 21.2, "GCC": 12.6, "GCA": 16.2, "GCG": 6.2,
    # Tyr
    "TAT": 18.8, "TAC": 14.8,
    # His
    "CAT": 13.6, "CAC": 7.8,
    # Gln
    "CAA": 27.3, "CAG": 12.1,
    # Asn
    "AAT": 35.7, "AAC": 24.8,
    # Lys
    "AAA": 41.9, "AAG": 30.8,
    # Asp
    "GAT": 37.6, "GAC": 20.2,
    # Glu
    "GAA": 45.6, "GAG": 19.2,
    # Cys
    "TGT": 8.1, "TGC": 4.8,
    # Trp
    "TGG": 10.4,
    # Arg
    "CGT": 6.4, "CGC": 2.6, "CGA": 3.0, "CGG": 1.7, "AGA": 21.3, "AGG": 9.2,
    # Gly
    "GGT": 23.9, "GGC": 9.8, "GGA": 10.9, "GGG": 6.0,
    # Stop
    "TAA": 1.1, "TAG": 0.5, "TGA": 0.7,
}

# Standard codon to amino acid mapping (one-letter). Stop codons map to "*".
CODON_TO_AA: dict = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L",
    "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L",
    "ATT": "I", "ATC": "I", "ATA": "I", "ATG": "M",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V",
    "TCT": "S", "TCC": "S", "TCA": "S", "TCG": "S",
    "CCT": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T",
    "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*",
    "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "AAT": "N", "AAC": "N", "AAA": "K", "AAG": "K",
    "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E",
    "TGT": "C", "TGC": "C", "TGA": "*", "TGG": "W",
    "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GGT": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}

# NNK codons: N in positions 1 and 2, K = G/T in position 3.
# 32 codons total, encoding all 20 amino acids plus the amber stop (TAG).
NNK_CODONS: list = [
    "AAG", "AAT", "ACG", "ACT", "AGG", "AGT", "ATG", "ATT",
    "CAG", "CAT", "CCG", "CCT", "CGG", "CGT", "CTG", "CTT",
    "GAG", "GAT", "GCG", "GCT", "GGG", "GGT", "GTG", "GTT",
    "TAG", "TAT", "TCG", "TCT", "TGG", "TGT", "TTG", "TTT",
]

# NNS codons: N in positions 1 and 2, S = C/G in position 3.
# 32 codons, encoding all 20 amino acids plus the amber stop (TAG).
NNS_CODONS: list = [
    "AAC", "AAG", "ACC", "ACG", "AGC", "AGG", "ATC", "ATG",
    "CAC", "CAG", "CCC", "CCG", "CGC", "CGG", "CTC", "CTG",
    "GAC", "GAG", "GCC", "GCG", "GGC", "GGG", "GTC", "GTG",
    "TAC", "TAG", "TCC", "TCG", "TGC", "TGG", "TTC", "TTG",
]

# Threshold (per 1000) below which a codon is considered rare in S. cerevisiae.
RARE_CODON_THRESHOLD: float = 10.0
