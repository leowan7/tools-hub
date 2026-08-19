"""Exception types Scout raises on purpose.

Imports nothing, so catching the type never drags the pipeline's numpy and
Bio imports into a module that only needs to name it.
"""


class ScoutInputError(ValueError):
    """The caller's input is wrong, and this message says how.

    Raised only where Scout has written a sentence for the person who
    uploaded the structure -- "Chain 'Z' not found in structure. Available
    chains: A, B". ``scout.routes._client_error`` forwards these verbatim to
    the browser and replaces every other exception with generic text, so the
    type is a promise about the message: no server paths, no filenames, no
    library internals.

    It DOES echo back the one field the message is about -- the chain id, or
    the requested residue list. That is the diagnostic, and the example above
    is itself an echo. Both renderers assign it with ``textContent``
    (``showAnalyzeError`` in ``templates/scout/index.html``, and the two error
    paths in ``templates/scout/feasibility.html``), so it lands as text and is
    never parsed as markup. A site echoing something WIDER than the field the
    user just typed needs that re-checked.

    It subclasses ``ValueError`` so the existing ``except ValueError``
    handlers keep catching it unchanged. They answer 422 for this type and
    500 for any other, because anything else is a server fault rather than a
    bad upload.

    **Do not raise it for server-side faults.** A missing reference dataset or
    a malformed cache is an operator's problem, not the uploader's; those keep
    raising plain ``ValueError`` and reach the user as the generic message,
    which is the honest answer -- there is nothing they can do about it.
    ``_parse_summary_csv`` in ``scout/epitope_db.py`` is the current example.
    """
