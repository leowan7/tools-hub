"""Platform API blueprint.

Exposes ``/api/v1/*`` for AI binder-design agents. The blueprint is only
registered in :mod:`app` when ``ENABLE_PLATFORM_API=1`` is set in the
process environment. With the flag off, the entire surface returns 404.
"""

from tools.platform_api.account_bp import platform_account_bp  # noqa: F401
from tools.platform_api.routes import platform_api_bp  # noqa: F401
