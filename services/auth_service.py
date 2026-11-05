"""Auth service — backward-compatible re-export.

The implementation lives in :mod:`services.auth` (facade
:class:`~services.auth.AuthService` composing focused mixins).
This module preserves the original import path for routers, factories,
sibling services, and tests — including the module-level names the
unit tests patch (``verify_password``, ``hash_password``,
``create_jwt_token``, ``get_system_config``,
``invalidate_must_change_password``, ``secrets``). The sub-package
resolves these through this namespace (deferred import) so patches
take effect.
"""

from __future__ import annotations

import secrets

from core.config import get_settings
from core.rbac import invalidate_must_change_password
from core.system_config import get_system_config
from services.auth import AuthService
from utils.crypto import create_jwt_token
from utils.password import hash_password, verify_password

__all__ = [
    "AuthService",
    "create_jwt_token",
    "get_settings",
    "get_system_config",
    "hash_password",
    "invalidate_must_change_password",
    "secrets",
    "verify_password",
]
