"""FastAPI dependencies for authentication and authorization.

Provides the following auth dependencies:

1. ``get_user_id`` — Optional auth.  Returns the user ID if authenticated,
   ``None`` otherwise.  Works with both API keys and JWT tokens.

2. ``require_user_id`` — Mandatory auth.  Raises 401 if not authenticated.

3. ``require_permission(permission)`` — Dependency factory.  Requires the
   authenticated principal to hold a specific permission string.  API keys
   are checked against ``request.state.api_key_permissions``; JWT users
   against their user role (``admin`` is wildcard) or their
   explicit ``permissions`` column (Redis-cached, fail-closed).  Raises
   403 if missing.

4. ``require_permission_or_self(permission)`` — Dependency factory.  The
   authenticated user may always access their own sub-resource; otherwise
   the same permission check as ``require_permission`` applies.

5. ``get_dashboard_user`` — Returns ``request.state.user_id`` if the
   request is authenticated via JWT (dashboard session), ``None`` otherwise.

6. ``get_current_user_id`` — Returns the authenticated user's UUID.
   Works with both JWT and API-key auth.  Raises 401 if not authenticated.

7. ``require_admin`` — Requires a JWT dashboard session AND the
   platform ``admin`` role.

8. ``require_admin_or_self`` — Dependency factory.  The caller may
   always act on their own user record; otherwise a DB-verified
   admin role is required (JWT-only, fail-closed).

9. ``get_actor_permissions`` — Returns the principal's grantable permission
   set (``None`` for the admin wildcard).  Routers pass it to
   services to cap permission grants; raises 503 when Redis is down.

All dependencies rely on ``request.state`` attributes set by
:class:`AuthMiddleware <openzync.middleware.auth.AuthMiddleware>`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)

from core.rbac import (
    get_effective_permissions,
    get_must_change_password,
    get_user_role,
    has_platform_manage,
)
from dependencies.db import get_db
from repositories.project_repository import ProjectRepository

# ═══════════════════════════════════════════════════════════════════════════════
# Bearer token scheme — auto-adds to OpenAPI docs
# ═══════════════════════════════════════════════════════════════════════════════

bearer_scheme = HTTPBearer(auto_error=False)
"""FastAPI ``HTTPBearer`` scheme with ``auto_error=False``.

When used as a dependency, this extracts the ``Authorization: Bearer <token>``
header and populates the OpenAPI docs with a security scheme.  Setting
``auto_error=False`` means it does not raise on missing headers — our
middleware handles that.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Must-change-password exemption
# ═══════════════════════════════════════════════════════════════════════════════

MUST_CHANGE_PASSWORD_EXEMPT_PATHS: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/auth/change-password"),
    ("GET", "/v1/auth/me"),
    ("POST", "/v1/auth/logout"),
    ("POST", "/v1/auth/refresh"),
})
"""Paths a user with ``must_change_password=True`` may still call.

The change-password endpoint itself, the profile read (so the UI can
discover the flag), logout, and refresh.  Everything else returns 403
until the password is changed.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Dependency functions
# ═══════════════════════════════════════════════════════════════════════════════


async def get_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),  # noqa: ARG001
) -> str | None:
    """Extract the authenticated user ID from the request state.

    The ``user_id`` is set by :class:`AuthMiddleware` after verifying the
    API key (key owner's UUID) or JWT token (``sub`` claim).  If no
    authentication was provided (public endpoint), this returns ``None``.

    The ``credentials`` parameter is included to ensure FastAPI parses the
    ``Authorization`` header and adds it to the OpenAPI schema.  Actual
    authentication is handled by the middleware.

    Args:
        request: The incoming HTTP request.
        credentials: Parsed Bearer credentials (used for OpenAPI schema only).

    Returns:
        The user UUID string, or ``None`` if not authenticated.
    """
    return getattr(request.state, "user_id", None)


async def require_user_id(
    user_id: str | None = Depends(get_user_id),
) -> str:
    """Require a valid authenticated user ID.

    Works with both API keys (key owner's UUID) and JWT tokens (``sub``).
    Raises a 401 error if the request has no valid authentication.
    Row ownership is enforced by the ``user_isolation`` RLS policies
    (``row.user_id = app.user_id``); an admin bypasses RLS via
    ``get_db_admin``.

    Args:
        user_id: The user ID from :func:`get_user_id`.

    Returns:
        The authenticated user UUID string.

    Raises:
        HTTPException: 401 if no valid authentication is present.
    """
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Authentication Required",
                "status": 401,
                "detail": (
                    "A valid API key or JWT token is required for this endpoint. "
                    "Provide it via the Authorization: Bearer <token> header."
                ),
            },
        )
    return user_id


# ═══════════════════════════════════════════════════════════════════════════════
# Permission-based authorization
# ═══════════════════════════════════════════════════════════════════════════════


async def _check_permission(
    request: Request,
    user_id: str,
    db: AsyncSession,
    required_permission: str,
) -> None:
    """Raise 403 unless the authenticated principal holds the permission.

    Dispatches on ``request.state.auth_type``:

    - **API key**: the key's permission list
      (``request.state.api_key_permissions``) must contain the required
      permission.
    - **JWT (dashboard user)**: the ``admin`` role is
      wildcard (implicitly holds every permission); any other role must
      carry the permission in their explicit ``permissions`` column
      (Redis-cached for 60 s, fail-closed to deny).  Ownership of the
      target rows is enforced by RLS, not here.

    Shared by :func:`require_permission` and
    :func:`require_permission_or_self` so both enforce the same decision.

    Args:
        request: The incoming HTTP request.
        user_id: The authenticated user ID.
        db: Request-scoped async DB session (role/permission lookups).
        required_permission: The permission string the principal must hold.

    Raises:
        HTTPException: 401 if a JWT session is missing a user id.
        HTTPException: 503 if Redis is not configured on app state.
        HTTPException: 403 if the principal lacks the permission.
    """
    auth_type: str | None = getattr(request.state, "auth_type", None)

    if auth_type == "api_key":
        key_permissions: list[str] = getattr(
            request.state, "api_key_permissions", []
        )
        if required_permission not in key_permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "type": "https://errors.openzync.tech/authorization_error",
                    "title": "Insufficient Permissions",
                    "status": 403,
                    "detail": (
                        f"Your API key does not have the required permission: "
                        f"'{required_permission}'.  "
                        f"Available permissions: {key_permissions}"
                    ),
                },
            )
        return

    # JWT (dashboard user)
    jwt_user_id: str | None = getattr(request.state, "user_id", None)
    if jwt_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Invalid Session",
                "status": 401,
                "detail": "The JWT token does not contain a valid user identifier.",
            },
        )

    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "https://errors.openzync.tech/service_unavailable",
                "title": "Service Unavailable",
                "status": 503,
                "detail": (
                    "The authorization cache is not configured. "
                    "Contact the administrator."
                ),
            },
        )

    role = await get_user_role(redis, db, UUID(user_id))
    if role == "admin":
        return

    permissions = await get_effective_permissions(redis, db, UUID(user_id))
    if required_permission not in permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "type": "https://errors.openzync.tech/authorization_error",
                "title": "Insufficient Permissions",
                "status": 403,
                "detail": (
                    f"This action requires the '{required_permission}' permission."
                ),
            },
        )


def require_permission(required_permission: str):
    """Dependency factory requiring a specific permission string.

    Works for both API keys (``request.state.api_key_permissions``) and
    JWT dashboard users (``admin`` role is wildcard,
    otherwise the user's explicit ``permissions`` column).  Fails closed:
    any lookup error denies with 403.  Ownership of the target rows is
    enforced by the ``user_isolation`` RLS policies
    (``row.user_id = token.sub``), not here.

    Args:
        required_permission: The permission string the principal must hold
            (e.g. ``"configuration:write"``, ``"members:read"``).

    Returns:
        A dependency callable that returns the ``user_id`` if the permission
        is held, or raises 401/403/503.
    """

    async def _permission_checker(
        request: Request,
        user_id: str = Depends(require_user_id),  # noqa: B008
        db: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> str:
        """Inner dependency that performs the permission check.

        Args:
            request: The incoming HTTP request.
            user_id: The authenticated user ID.
            db: Request-scoped async DB session (role/permission lookups).

        Returns:
            The ``user_id`` if the permission check passes.

        Raises:
            HTTPException: 401/403/503 via :func:`_check_permission`.
        """
        await _check_permission(request, user_id, db, required_permission)
        return user_id

    return _permission_checker


async def require_user(
    request: Request,
    user_id: str = Depends(require_user_id),  # noqa: B008
) -> str:
    """Require authentication and return the caller's user ID.

    Ownership check helper: compare the returned ID against the target
    row's ``user_id`` (``row.user_id == token.sub``).  Admins bypass
    via ``get_db_admin`` instead of this dependency.
    """
    return user_id


def require_permission_or_self(required_permission: str):
    """Dependency factory requiring the permission, or acting on oneself.

    The authenticated user may always access their **own** sub-resource
    (``str(user_id) == request.state.user_id``); otherwise the same
    permission check as :func:`require_permission` applies.  Intended for
    per-user sub-resources (e.g. ``GET /users/{user_id}/summary``).

    Args:
        required_permission: The permission string required for acting on
            another user's resource.

    Returns:
        A dependency callable that returns the caller's ``user_id``.
    """

    async def _permission_or_self_checker(
        request: Request,
        user_id: UUID = Path(...),  # noqa: B008
        caller_id: str = Depends(require_user_id),  # noqa: B008
        db: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> str:
        """Inner dependency that performs the self-or-permission check.

        Args:
            request: The incoming HTTP request.
            user_id: The target user's UUID (path param).
            caller_id: The authenticated caller's user ID.
            db: Request-scoped async DB session (role/permission lookups).

        Returns:
            The caller's ``user_id`` if the caller is the target user or
            holds the required permission.

        Raises:
            HTTPException: 401/403/503 via :func:`_check_permission`.
        """
        # Self-bypass is JWT-session only: API keys authenticate as the
        # key owner's UUID, and must not reach personal endpoints via a
        # creator identity.
        auth_type: str | None = getattr(request.state, "auth_type", None)
        actor_user_id: str | None = getattr(request.state, "user_id", None)
        if (
            auth_type == "jwt"
            and actor_user_id is not None
            and str(user_id) == actor_user_id
        ):
            return caller_id
        await _check_permission(request, caller_id, db, required_permission)
        return caller_id

    return _permission_or_self_checker


async def get_actor_permissions(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> frozenset[str] | None:
    """Resolve the calling principal's grantable permission set.

    Used to cap permission grants so a principal can never escalate beyond
    its own set.  API keys resolve from
    ``request.state.api_key_permissions`` (always populated by
    :class:`AuthMiddleware` — ``None`` is treated as an empty set,
    fail-closed).  JWT sessions resolve via the user role
    (``admin`` is wildcard) or the cached explicit
    permission set.

    Args:
        request: The incoming HTTP request.
        db: Request-scoped async DB session (role/permission lookups).

    Returns:
        ``None`` for the admin (uncapped — wildcard), otherwise the
        actor's permission set used to cap grants.

    Raises:
        HTTPException: 401 if a JWT session is missing a user id or org id.
        HTTPException: 503 if Redis is not configured on app state.
    """
    if getattr(request.state, "auth_type", None) == "api_key":
        key_permissions: list[str] | None = getattr(
            request.state, "api_key_permissions", None
        )
        return frozenset(key_permissions or [])

    user_id: str | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Invalid Session",
                "status": 401,
                "detail": "The JWT token does not contain a valid user identifier.",
            },
        )

    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "https://errors.openzync.tech/service_unavailable",
                "title": "Service Unavailable",
                "status": 503,
                "detail": (
                    "The authorization cache is not configured. "
                    "Contact the administrator."
                ),
            },
        )

    role = await get_user_role(redis, db, UUID(user_id))
    if role == "admin":
        return None
    return await get_effective_permissions(redis, db, UUID(user_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard-specific dependencies
# ═══════════════════════════════════════════════════════════════════════════════


async def get_dashboard_user(
    request: Request,
    user_id: str = Depends(require_user_id),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> str:
    """Require a JWT-authenticated dashboard user.

    Returns the ``user_id`` from the JWT claims.  Raises 401 if the
    request is authenticated via API key instead of JWT.

    **Must-change-password gate**: after resolving the user, if the DB
    row has ``must_change_password=True`` and the request path is not in
    :data:`MUST_CHANGE_PASSWORD_EXEMPT_PATHS`, a 403 is raised — the
    seeded root credential must be replaced before the dashboard is
    usable.  API-key auth paths are unaffected.  The flag is read from
    the DB (Redis-cached briefly, fail-closed to "must change") via
    :func:`core.rbac.get_must_change_password`.

    Args:
        request: The incoming HTTP request.
        user_id: The authenticated user ID (from ``require_user_id``).
        db: Request-scoped async DB session (flag lookup).

    Returns:
        The dashboard user's UUID string.

    Raises:
        HTTPException: 401 if not a JWT-authenticated session.
        HTTPException: 403 if the user must change their password and the
            path is not exempt.
    """
    auth_type: str | None = getattr(request.state, "auth_type", None)
    if auth_type != "jwt":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Dashboard Authentication Required",
                "status": 401,
                "detail": (
                    "This endpoint requires a JWT token (dashboard session). "
                    "API key authentication is not sufficient."
                ),
            },
        )

    user_id: str | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Invalid Session",
                "status": 401,
                "detail": "The JWT token does not contain a valid user identifier.",
            },
        )

    # ── Must-change-password gate (JWT dashboard sessions only) ──────────
    # Fail closed: absent Redis (or a lookup error inside
    # get_must_change_password, which degrades to "must change") must never
    # let a session through that the DB would have blocked.
    path = request.url.path
    if (request.method, path) not in MUST_CHANGE_PASSWORD_EXEMPT_PATHS:
        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "type": "https://errors.openzync.tech/service_unavailable",
                    "title": "Service Unavailable",
                    "status": 503,
                    "detail": (
                        "The authorization cache is not configured. "
                        "Contact the administrator."
                    ),
                },
            )
        must_change = await get_must_change_password(
            redis,
            db,
            UUID(user_id),
        )
        if must_change:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "type": "https://errors.openzync.tech/authorization_error",
                        "title": "Password Change Required",
                        "status": 403,
                        "detail": (
                            "You must set a new password before using the "
                            "dashboard."
                        ),
                    },
                )

    return user_id


async def get_current_user_id(
    request: Request,
    user_id: str = Depends(require_user_id),
) -> UUID:
    """Require an authenticated user and return their UUID.

    Works with both JWT tokens and API keys.  The user ID is extracted
    from ``request.state.user_id`` (set by :class:`AuthMiddleware`).

    Use this dependency in project-scoped endpoints to obtain the
    ``created_by`` value for attribution.

    Args:
        request: The incoming HTTP request.
        user_id: The authenticated user ID (from ``require_user_id``).

    Returns:
        The authenticated user's UUID.

    Raises:
        HTTPException: 401 if the user is not authenticated.
    """
    user_id: str | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Authentication Required",
                "status": 401,
                "detail": (
                    "A valid user session or API key is required for this endpoint. "
                    "Provide it via the Authorization: Bearer <token> header."
                ),
            },
        )
    return UUID(user_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Platform admin
# ═══════════════════════════════════════════════════════════════════════════════


async def _ensure_admin(
    request: Request,
    user_id: str,
    db: AsyncSession,
) -> None:
    """Raise 403 unless the JWT session is a DB-verified admin.

    Requires both of:
    1. A JWT dashboard session (``get_dashboard_user`` has already raised
       401 otherwise).
    2. A DB-verified role of ``admin`` (Redis-cached, fail-closed to
       ``member``) — the JWT ``role`` claim is never trusted.

    Fail-closed: any Redis/DB error degrades the role lookup to
    ``member``, which denies.  This is the gate that authorises the RLS
    bypass in ``get_db_admin`` — no admin check, no bypass.

    Args:
        request: The incoming HTTP request (for ``app.state.redis``).
        user_id: The authenticated dashboard user's UUID string.
        db: Request-scoped async DB session (role lookup).

    Returns:
        None.

    Raises:
        HTTPException: 503 if Redis is not configured on app state.
        HTTPException: 403 if the role is not ``admin``.
    """
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "https://errors.openzync.tech/service_unavailable",
                "title": "Service Unavailable",
                "status": 503,
                "detail": (
                    "The authorization cache is not configured. "
                    "Contact the administrator."
                ),
            },
        )
    role = await get_user_role(redis, db, UUID(user_id))
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "type": "https://errors.openzync.tech/authorization_error",
                "title": "Insufficient Permissions",
                "status": 403,
                "detail": "This action requires the platform admin role.",
            },
        )


async def require_admin(
    request: Request,
    user_id: str = Depends(get_dashboard_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> str:
    """Require a JWT dashboard session with the platform admin role.

    Raises 401 for API-key auth or a missing user id (via
    :func:`get_dashboard_user`), 403 for any non-``admin`` role or a
    user who must still change their password.  The gate is role-only —
    there is no org check (single platform org).  The role is verified
    against the DB (Redis-cached for 60 s, fail-closed) — never trusted
    from the JWT ``role`` claim.

    Args:
        request: The incoming HTTP request.
        user_id: The authenticated user ID (from ``require_user_id``).
        user_id: The dashboard user's UUID string (from ``get_dashboard_user``).
        db: Request-scoped async DB session.

    Returns:
        The authenticated admin's UUID string.

    Raises:
        HTTPException: 401 if not a JWT-authenticated session.
        HTTPException: 403 if not a platform admin.
    """
    await _ensure_admin(request, user_id, db)
    return user_id


def require_admin_or_self():
    """Dependency factory requiring admin, or acting on oneself.

    JWT-only via :func:`get_dashboard_user` (API keys raise 401).  The
    caller may always act on their own user record
    (``str(user_id) == caller_id``); otherwise a DB-verified
    ``admin`` role is required via :func:`_ensure_admin`
    (fail-closed — the JWT ``role`` claim is never trusted).  Intended
    for user-management endpoints (``GET/PATCH /v1/users/{user_id}``)
    where self-read/self-edit is allowed but cross-user access is a
    admin operation.

    Returns:
        A dependency callable that returns the caller's ``user_id``.
    """

    async def _admin_or_self_checker(
        request: Request,
        user_id: UUID = Path(...),  # noqa: B008
        caller_id: str = Depends(get_dashboard_user),  # noqa: B008
        db: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> str:
        """Inner dependency that performs the self-or-admin check.

        Args:
            request: The incoming HTTP request.
            user_id: The target user's UUID (path param).
            caller_id: The authenticated dashboard user's ID.
            db: Request-scoped async DB session (role lookup).

        Returns:
            The caller's ``user_id`` if the caller is the target user or
            a DB-verified admin.

        Raises:
            HTTPException: 401 for non-JWT auth, 403 for a non-admin
                acting on another user, 503 if Redis is not configured.
        """
        if str(user_id) == caller_id:
            return caller_id
        await _ensure_admin(request, caller_id, db)
        return caller_id

    return _admin_or_self_checker


async def resolve_platform_scope(
    request: Request,
    db: AsyncSession,
    caller_id: UUID,
    target_user_id: UUID | None,
    project_id: UUID | None,
) -> tuple[UUID, bool]:
    """Gate the ``user_id``/``project_id`` filters on platform read endpoints.

    ``platform:manage`` holders (and the admin wildcard) read site-wide
    by default; everyone else stays caller-scoped:

    - Both filters together → 422.
    - ``target_user_id`` from a non-holder → 403; from a holder it
      scopes the read to that user (under an RLS bypass at the call site).
    - ``project_id`` from a holder scopes the site-wide read to that
      project.  From a non-holder the project must be caller-owned
      (single owner query — members of shared projects get 403 here;
      their rows stay visible through the unfiltered caller scope).
    - Neither filter: holders go site-wide, others stay caller-scoped.

    Args:
        request: The incoming HTTP request (app-state Redis).
        db: Request-scoped async DB session (role/permission/project reads).
        caller_id: The authenticated caller's UUID.
        target_user_id: Optional ``user_id`` query filter (holder-only).
        project_id: Optional project filter (owner-verified for non-holders).

    Returns:
        ``(effective_user_id, sitewide)`` — the user scope to query and
        whether to use the site-wide repository methods (callers wrap
        those in :func:`core.rls.rls_bypass`).

    Raises:
        HTTPException: 422 if both filters are supplied, 403 if a
            non-holder filters by user or by a project they do not own.
    """
    if target_user_id is not None and project_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pass either user_id or project_id, not both.",
        )
    redis = getattr(request.app.state, "redis", None)
    holder = await has_platform_manage(redis, db, caller_id)
    if target_user_id is not None:
        if not holder:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "type": "https://errors.openzync.tech/authorization_error",
                    "title": "Insufficient Permissions",
                    "status": 403,
                    "detail": (
                        "Filtering by user_id requires the "
                        "'platform:manage' permission."
                    ),
                },
            )
        return target_user_id, False
    if project_id is not None and not holder:
        project = await ProjectRepository(db).get_by_id(caller_id, project_id)
        # ⚠️ owner-only: active members of a shared project 403 here —
        # their own rows remain visible through the unfiltered scope.
        if project is None or project.user_id != caller_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "type": "https://errors.openzync.tech/authorization_error",
                    "title": "Insufficient Permissions",
                    "status": 403,
                    "detail": "This project does not belong to the caller.",
                },
            )
    return caller_id, holder
