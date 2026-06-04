"""Auth service.

The auth implementation lives in ``_core``; this package re-exports it so
``from awm.services import auth`` (and ``auth.local_token`` /
``auth._token_cache``) keeps working after middleware modules moved here
as submodules (``middleware_auth``, ``middleware_gate``, ``ws_auth``).
"""

from awm.services.auth._core import *  # noqa: F401,F403
from awm.services.auth._core import _token_cache, _challenges  # noqa: F401  (private — used by test fixtures)
