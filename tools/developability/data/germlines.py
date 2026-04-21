"""Human germline V-gene reference sequences from IMGT.

A minimal set of well-used human heavy and light chain germline V-gene
sequences used as the reference pool for k-mer based humanness scoring.
Sequences are framework regions only (CDRs omitted from germline for
fairer comparison of framework humanness).

For a production-grade tool, replace this inline set with a full IMGT
germline repertoire (hundreds of IGHV/IGKV/IGLV alleles) or use BioPhi's
OASis pipeline.

Source: IMGT/GENE-DB, representative alleles.
"""

# Heavy chain germline V-regions (IGHV). Sequences span FR1-CDR1-FR2-CDR2-FR3
# as the canonical V-gene translation (ends before CDR3/J-region).

IGHV_GERMLINES: dict = {
    "IGHV1-46*01": (
        "QVQLVQSGAEVKKPGASVKVSCKASGYTFTSYYMHWVRQAPGQGLEWMGIINPSGGSTSY"
        "AQKFQGRVTMTRDTSTSTVYMELSSLRSEDTAVYYCAR"
    ),
    "IGHV3-23*01": (
        "EVQLLESGGGLVQPGGSLRLSCAASGFTFSSYAMSWVRQAPGKGLEWVSAISGSGGSTYY"
        "ADSVKGRFTISRDNSKNTLYLQMNSLRAEDTAVYYCAK"
    ),
    "IGHV3-30*03": (
        "QVQLVESGGGVVQPGRSLRLSCAASGFTFSSYAMHWVRQAPGKGLEWVAVISYDGSNKYY"
        "ADSVKGRFTISRDNSKNTLYLQMNSLRAEDTAVYYCAR"
    ),
    "IGHV4-34*01": (
        "QVQLQQWGAGLLKPSETLSLTCAVYGGSFSGYYWSWIRQPPGKGLEWIGEINHSGSTNYN"
        "PSLKSRVTISVDTSKNQFSLKLSSVTAADTAVYYCAR"
    ),
    "IGHV1-69*01": (
        "QVQLVQSGAEVKKPGSSVKVSCKASGGTFSSYAISWVRQAPGQGLEWMGGIIPIFGTANY"
        "AQKFQGRVTITADESTSTAYMELSSLRSEDTAVYYCAR"
    ),
}

# Kappa light chain germline V-regions (IGKV).

IGKV_GERMLINES: dict = {
    "IGKV1-39*01": (
        "DIQMTQSPSSLSASVGDRVTITCRASQSISSYLNWYQQKPGKAPKLLIYAASSLQSGVPS"
        "RFSGSGSGTDFTLTISSLQPEDFATYYC"
    ),
    "IGKV3-20*01": (
        "EIVLTQSPGTLSLSPGERATLSCRASQSVSSSYLAWYQQKPGQAPRLLIYGASSRATGIP"
        "DRFSGSGSGTDFTLTISRLEPEDFAVYYC"
    ),
    "IGKV4-1*01": (
        "DIVMTQSPDSLAVSLGERATINCKSSQSVLYSSNNKNYLAWYQQKPGQPPKLLIYWASTR"
        "ESGVPDRFSGSGSGTDFTLTISSLQAEDVAVYYC"
    ),
}

# Lambda light chain germline V-regions (IGLV).

IGLV_GERMLINES: dict = {
    "IGLV1-44*01": (
        "QSVLTQPPSASGTPGQRVTISCSGSSSNIGSNTVNWYQQLPGTAPKLLIYSNNQRPSGVP"
        "DRFSGSKSGTSASLAISGLQSEDEADYYC"
    ),
    "IGLV2-14*01": (
        "QSALTQPASVSGSPGQSITISCTGTSSDVGGYNYVSWYQQHPGKAPKLMIYDVSNRPSGV"
        "SNRFSGSKSGNTASLTISGLQAEDEADYYC"
    ),
    "IGLV3-21*01": (
        "SYELTQPPSVSVSPGQTASITCSGDKLGDKYACWYQQKPGQSPVLVIYQDSKRPSGIPER"
        "FSGSNSGNTATLTISGTQAMDEADYYC"
    ),
}


def get_germlines_for_chain(chain_type: str) -> dict:
    """Return germline dictionary matching the requested chain type.

    Args:
        chain_type: Chain identifier. "VH"/"HEAVY"/"VHH" for heavy chain,
            "VK"/"KAPPA" for kappa light, "VL"/"LAMBDA" for lambda light.
            For unknown chains (including "SCFV", "OTHER"), a union of
            heavy + kappa + lambda is returned so humanness can still
            be estimated.

    Returns:
        Dictionary mapping germline name to amino acid string.
    """
    normalized = chain_type.strip().upper()
    if normalized in {"VH", "HEAVY", "H", "VHH"}:
        return IGHV_GERMLINES
    if normalized in {"VK", "KAPPA", "K"}:
        return IGKV_GERMLINES
    if normalized in {"VL", "LAMBDA", "L"}:
        return IGLV_GERMLINES
    # Unknown chain: return full pool so humanness can still be estimated.
    union: dict = {}
    union.update(IGHV_GERMLINES)
    union.update(IGKV_GERMLINES)
    union.update(IGLV_GERMLINES)
    return union
