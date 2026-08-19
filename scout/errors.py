"""Exception types Scout raises on purpose.

Kept in its own module so :mod:`scout.pipeline` and :mod:`scout.routes` can
both import it without an import cycle. It imports nothing itself.
"""


class ScoutInputError(ValueError):
    """The caller's input is wrong, and this message says how.

    Raised only where Scout has written a sentence for the person who
    uploaded the structure -- "Chain 'Z' not found in structure. Available
    chains: A, B". ``scout.routes._client_error`` forwards these verbatim to
    the browser and replaces every other exception with generic text, so the
    type is a promise about the message: no server paths, no filenames, no
    library internals, no caller input echoed back.

    It subclasses ``ValueError`` so the ``except ValueError`` handlers that
    answer 422 keep catching it unchanged.

    **Do not raise it for server-side faults.** A missing reference dataset
    or a malformed cache is an operator's problem, not the uploader's; those
    keep raising plain ``ValueError`` and reach the user as the generic
    message, which is the honest answer -- there is nothing they can do
    about it. ``scout/epitope_db.py`` is the current example.
    """
