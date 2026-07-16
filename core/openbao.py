"""Async OpenBao (Vault-compatible) HTTP client.

Uses raw ``httpx.AsyncClient`` against the OpenBao REST API for AppRole
authentication, KV v2 secret management, namespace lifecycle management,
and system-level / per-org configuration read/write.

Usage::

    async with OpenBaoClient(addr, role_id, secret_id) as bao:
        config = await bao.read_system_config()
        await bao.write_org_config(org_id, {"llm_api_key": "sk-..."})
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, overload

import httpx

if TYPE_CHECKING:
    from uuid import UUID

from core.openbao_exceptions import (
    OpenBaoAuthError,
    OpenBaoConnectionError,
    OpenBaoError,
    OpenBaoNamespaceError,
    OpenBaoRateLimitError,
    OpenBaoSecretNotFoundError,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

ORG_NAMESPACE_PREFIX = "org_"
"""Prefix applied to organisation namespace names."""

KV_MOUNT = "config"
"""KV v2 mount path for configuration secrets."""

SYSTEM_KEY_MAPPING: dict[str, str] = {
    "database_url": "OZ_DATABASE_URL",
    "redis_url": "OZ_REDIS_URL",
    "secret_key": "OZ_SECRET_KEY",
    "webhook_signing_secret": "OZ_WEBHOOK_SIGNING_SECRET",
    "prometheus_url": "OZ_PROMETHEUS_URL",
    "environment": "OZ_ENVIRONMENT",
    "log_level": "OZ_LOG_LEVEL",
    "cors_origins": "OZ_CORS_ORIGINS",
    "hosts_allowed": "OZ_HOSTS_ALLOWED",
    "max_workers": "OZ_MAX_WORKERS",
    "jwt_access_token_ttl_minutes": "OZ_JWT_ACCESS_TOKEN_TTL_MINUTES",
    "jwt_refresh_token_ttl_days": "OZ_JWT_REFRESH_TOKEN_TTL_DAYS",
    "falkordb_url": "OZ_FALKORDB_URL",
    "falkordb_max_connections": "OZ_FALKORDB_MAX_CONNECTIONS",
    "falkordb_socket_timeout": "OZ_FALKORDB_SOCKET_TIMEOUT",
    "rate_limit_ip_max": "OZ_RATE_LIMIT_IP_MAX",
    "rate_limit_window_sec": "OZ_RATE_LIMIT_WINDOW_SEC",
    "prompt_caching_enabled": "OZ_PROMPT_CACHING_ENABLED",
    "prompt_caching_anthropic_min_tokens": "OZ_PROMPT_CACHING_ANTHROPIC_MIN_TOKENS",
    "prompt_caching_anthropic_ttl": "OZ_PROMPT_CACHING_ANTHROPIC_TTL",
    "smtp_host": "OZ_SMTP_HOST",
    "smtp_port": "OZ_SMTP_PORT",
    "smtp_username": "OZ_SMTP_USERNAME",
    "smtp_password": "OZ_SMTP_PASSWORD",
    "smtp_from_addr": "OZ_SMTP_FROM_ADDR",
    "smtp_use_tls": "OZ_SMTP_USE_TLS",
    "smtp_start_tls": "OZ_SMTP_START_TLS",
}
"""Maps OpenBao config key names (snake_case) to ``OZ_`` environment variable names."""

_MAX_RETRIES = 3
"""Maximum number of times to retry a request that receives a 429 (rate-limited)."""


# ═══════════════════════════════════════════════════════════════════════════════
# Client
# ═══════════════════════════════════════════════════════════════════════════════


class OpenBaoClient:
    """Async HTTP client for the OpenBao secrets-management API.

    Authenticates via AppRole (``role_id`` + ``secret_id``) and exposes
    methods for KV v2 operations, namespace management, and configuration
    read/write for both system-level and per-org secrets.

    Must be used as an async context manager::

        async with OpenBaoClient(addr, role_id, secret_id, timeout=15.0) as bao:
            data = await bao._kv_read("config/data/mykey")

    Attributes:
        addr: OpenBao server URL (e.g. ``http://localhost:8200``).
        role_id: AppRole RoleID for authentication.
        secret_id: AppRole SecretID for authentication.
        timeout: Default HTTP request timeout in seconds.
    """

    def __init__(
        self,
        addr: str,
        role_id: str,
        secret_id: str,
        *,
        timeout: float = 10.0,
        namespace: str = "system/",
    ) -> None:
        """Initialise the client — does not connect until entering the context.

        Args:
            addr: OpenBao server URL.
            role_id: AppRole RoleID.
            secret_id: AppRole SecretID.
            timeout: HTTP request timeout in seconds (default 10).
            namespace: OpenBao namespace for auth and API requests
                       (default ``system/``, pass ``""`` for root).
        """
        self.addr = addr
        self.role_id = role_id
        self.secret_id = secret_id
        self.timeout = timeout
        self._namespace = namespace
        self._client_token: str | None = None
        self._http: httpx.AsyncClient | None = None
        self._token_expires_at: float | None = None

    # ── Async context manager ───────────────────────────────────────────────

    async def __aenter__(self) -> OpenBaoClient:
        """Enter async context: create the HTTP client and authenticate.

        Returns:
            Self, ready for use.

        Raises:
            OpenBaoConnectionError: If OpenBao is unreachable.
            OpenBaoAuthError: If authentication fails.
        """
        self._http = httpx.AsyncClient(
            base_url=self.addr,
            timeout=self.timeout,
            http2=True,
        )
        await self._authenticate()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit async context: close the HTTP client."""
        if self._http is not None:
            await self._http.aclose()

    # ── Authentication ──────────────────────────────────────────────────────

    async def _authenticate(self) -> None:
        """Authenticate with OpenBao via the AppRole login endpoint.

        POST ``/v1/auth/approle/login`` with ``role_id`` and ``secret_id``
        and stores the returned ``client_token`` for all subsequent requests.

        Raises:
            OpenBaoConnectionError: If OpenBao is unreachable or the request
                times out.
            OpenBaoAuthError: If credentials are invalid or the response
                is malformed.
        """
        if self._http is None:
            raise OpenBaoConnectionError(
                "HTTP client not initialised; use 'async with'",
            )

        try:
            headers: dict[str, str] = {}
            if self._namespace:
                headers["X-Vault-Namespace"] = self._namespace
            resp = await self._http.post(
                "/v1/auth/approle/login",
                headers=headers,
                json={"role_id": self.role_id, "secret_id": self.secret_id},
            )
        except httpx.ConnectError as e:
            raise OpenBaoConnectionError(
                f"Cannot connect to OpenBao at {self.addr}: {e}",
            ) from e
        except httpx.TimeoutException as e:
            raise OpenBaoConnectionError(
                f"Timeout connecting to OpenBao at {self.addr}: {e}",
            ) from e

        if resp.status_code != 200:
            try:
                body = resp.json()
                errors = body.get("errors", [])
                msg = "; ".join(errors) if errors else resp.reason_phrase
            except Exception:
                msg = resp.reason_phrase or str(resp.status_code)
            raise OpenBaoAuthError(f"[auth/approle/login] AppRole login failed: {msg}")

        try:
            body = resp.json()
            self._client_token = body["auth"]["client_token"]
            # Calculate token expiry threshold (renew at 80% of TTL)
            lease_duration = body.get("auth", {}).get("lease_duration", 3600)
            self._token_expires_at = time.monotonic() + lease_duration * 0.8
        except (KeyError, TypeError, ValueError) as e:
            raise OpenBaoAuthError(
                f"[auth/approle/login] Response missing 'auth.client_token': {e}",
            ) from e

    @property
    def _token(self) -> str:
        """Return the current client token.

        Raises:
            OpenBaoAuthError: If not yet authenticated.
        """
        if self._client_token is None:
            raise OpenBaoAuthError("Not authenticated to OpenBao — use 'async with'")
        return self._client_token

    def _headers(self, namespace: str | None = None) -> dict[str, str]:
        """Build request headers with the auth token and optional namespace.

        Args:
            namespace: Optional OpenBao namespace path (e.g. ``org_<uuid>/``).

        Returns:
            Headers dict with ``X-Vault-Token`` and optionally
            ``X-Vault-Namespace``.
        """
        headers: dict[str, str] = {"X-Vault-Token": self._token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        return headers

    async def _ensure_auth(self) -> None:
        """Re-authenticate if the current token is near or past its TTL.

        Uses monotonic time (no API call) so this is effectively free in
        the common case. Only triggers a real auth call at ~80% of TTL.
        """
        if self._token_expires_at is not None and time.monotonic() >= self._token_expires_at:
            logger.info("OpenBao token near expiry \u2014 re-authenticating")
            await self._authenticate()

    # ── Low-level request helper ────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        namespace: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send an authenticated request to the OpenBao REST API.

        Wraps network-level errors in :class:`OpenBaoConnectionError` and
        delegates HTTP status-code handling to :meth:`_raise_on_error`.
        Retries up to :data:`_MAX_RETRIES` times on ``OpenBaoRateLimitError``
        with exponential backoff.

        Args:
            method: HTTP method (``GET``, ``POST``, ``LIST``, ``DELETE``, …).
            path: API path relative to ``/v1/`` (e.g. ``config/data/mykey``).
            namespace: Optional namespace header value.
            **kwargs: Extra arguments forwarded to
                :meth:`httpx.AsyncClient.request`.

        Returns:
            The HTTP response (status 200 only — errors raise).

        Raises:
            OpenBaoConnectionError: On network / timeout errors.
            OpenBaoAuthError: On 401 or 403.
            OpenBaoSecretNotFoundError: On 404.
            OpenBaoNamespaceError: On 412.
            OpenBaoRateLimitError: On 429 (raised after retries are exhausted).
            OpenBaoError: On any other non-200 status.
        """
        if self._http is None:
            raise OpenBaoConnectionError(
                "HTTP client not initialised; use 'async with'",
            )

        await self._ensure_auth()
        url = f"/v1/{path.lstrip('/')}"

        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._http.request(
                    method,
                    url,
                    headers=self._headers(namespace),
                    **kwargs,
                )
            except httpx.ConnectError as e:
                raise OpenBaoConnectionError(
                    f"Cannot connect to OpenBao at {self.addr}: {e}",
                ) from e
            except httpx.TimeoutException as e:
                raise OpenBaoConnectionError(
                    f"Timeout connecting to OpenBao at {self.addr}: {e}",
                ) from e

            try:
                self._raise_on_error(resp, path)
                return resp
            except OpenBaoRateLimitError:
                if attempt < _MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "Rate limited by OpenBao, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

    # ═════════════════════════════════════════════════════════════════════════
    # Static error mapper
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _raise_on_error(resp: httpx.Response, path: str) -> None:
        """Map an HTTP response status to a typed OpenBao exception.

        Args:
            resp: The HTTP response to inspect.
            path: The API path for context in error messages.

        Raises:
            OpenBaoAuthError: If status is 401 or 403.
            OpenBaoSecretNotFoundError: If status is 404.
            OpenBaoNamespaceError: If status is 412.
            OpenBaoRateLimitError: If status is 429.
            OpenBaoConnectionError: For any 5xx status.
            OpenBaoError: For any other non-200 status.
        """
        if resp.status_code in (200, 204):
            return

        try:
            body = resp.json()
            errors = body.get("errors", [])
            msg = "; ".join(errors) if errors else resp.reason_phrase
        except Exception:
            msg = resp.reason_phrase or str(resp.status_code)

        if resp.status_code in (401, 403):
            raise OpenBaoAuthError(f"[{path}] {msg}")
        if resp.status_code == 404:
            raise OpenBaoSecretNotFoundError(f"[{path}] {msg}")
        if resp.status_code == 412:
            raise OpenBaoNamespaceError(f"[{path}] {msg}")
        if resp.status_code == 429:
            raise OpenBaoRateLimitError(f"[{path}] {msg}")
        if 500 <= resp.status_code < 600:
            raise OpenBaoConnectionError(
                f"[{path}] Server error ({resp.status_code}): {msg}",
            )
        raise OpenBaoError(f"[{path}] HTTP {resp.status_code}: {msg}")

    # ═════════════════════════════════════════════════════════════════════════
    # KV v2 low-level operations
    # ═════════════════════════════════════════════════════════════════════════

    @overload
    async def _kv_read(
        self,
        path: str,
        namespace: str | None = None,
        *,
        include_meta: bool = False,
    ) -> dict[str, Any]: ...

    @overload
    async def _kv_read(
        self,
        path: str,
        namespace: str | None = None,
        *,
        include_meta: bool = True,
    ) -> tuple[dict[str, Any], int]: ...

    async def _kv_read(
        self,
        path: str,
        namespace: str | None = None,
        *,
        include_meta: bool = False,
    ) -> dict[str, Any] | tuple[dict[str, Any], int]:
        """Read a secret from a KV v2 engine.

        Args:
            path: Full KV path including mount (e.g. ``config/data/database_url``).
            namespace: Optional namespace.
            include_meta: If ``True``, also return the version number as a
                second element for CAS-aware writes.

        Returns:
            The ``data.data`` dict from the response, or a ``(data, version)``
            tuple when ``include_meta`` is ``True``.

        Raises:
            OpenBaoSecretNotFoundError: If the path does not exist.
        """
        resp = await self._request("GET", path, namespace=namespace)
        body = resp.json()
        data = body["data"]["data"]
        if include_meta:
            version = body["data"].get("metadata", {}).get("version", 0)
            return data, version
        return data

    async def _kv_write(
        self,
        path: str,
        data: dict[str, Any],
        namespace: str | None = None,
        *,
        cas_version: int | None = None,
    ) -> None:
        """Write a secret to a KV v2 engine.

        Args:
            path: Full KV path including mount (e.g. ``config/data/database_url``).
            data: The secret data to persist.
            namespace: Optional namespace.
            cas_version: If set, the write will only succeed if the current
                version matches (Compare-And-Swap).  Requires KV v2.
        """
        options: dict[str, Any] = {}
        if cas_version is not None:
            options["cas"] = cas_version
        await self._request(
            "POST",
            path,
            namespace=namespace,
            json={"data": data, "options": options},
        )

    async def _kv_list(
        self,
        path: str,
        namespace: str | None = None,
    ) -> list[str]:
        """List secret keys at a KV v2 metadata path.

        Args:
            path: Full KV metadata path (e.g. ``config/metadata/``).
            namespace: Optional namespace.

        Returns:
            List of key names at the given path.

        Raises:
            OpenBaoSecretNotFoundError: If the path does not exist.
        """
        resp = await self._request("LIST", path, namespace=namespace)
        body = resp.json()
        return body["data"]["keys"]

    async def _kv_delete(
        self,
        path: str,
        namespace: str | None = None,
    ) -> None:
        """Delete a secret from a KV v2 engine.

        Args:
            path: Full KV path including mount (e.g. ``config/data/database_url``).
            namespace: Optional namespace.
        """
        await self._request("DELETE", path, namespace=namespace)

    # ═════════════════════════════════════════════════════════════════════════
    # Namespace management
    # ═════════════════════════════════════════════════════════════════════════

    async def create_namespace(self, name: str) -> None:
        """Create a new OpenBao namespace.

        If the namespace already exists, the operation is silently ignored
        (HTTP 400 with ``"already exists"`` is treated as a no-op).

        Args:
            name: Namespace name (e.g. ``org_<uuid>``).

        Raises:
            OpenBaoConnectionError: On network errors.
            OpenBaoAuthError: On 401/403.
        """
        if self._http is None:
            raise OpenBaoConnectionError(
                "HTTP client not initialised; use 'async with'",
            )

        path = f"sys/namespaces/{name}"
        try:
            resp = await self._http.post(
                f"/v1/{path}",
                headers=self._headers(),
            )
        except httpx.ConnectError as e:
            raise OpenBaoConnectionError(
                f"Cannot connect to OpenBao at {self.addr}: {e}",
            ) from e
        except httpx.TimeoutException as e:
            raise OpenBaoConnectionError(
                f"Timeout connecting to OpenBao at {self.addr}: {e}",
            ) from e

        # 400 with "already exists" is expected during initialisation.
        if resp.status_code == 400:
            logger.info("Namespace %r already exists — ignored", name)
            return

        self._raise_on_error(resp, path)

    async def delete_namespace(self, name: str) -> None:
        """Delete an OpenBao namespace and all its contents.

        Args:
            name: Namespace name to delete.

        Raises:
            OpenBaoSecretNotFoundError: If the namespace does not exist.
        """
        await self._request("DELETE", f"sys/namespaces/{name}")

    async def enable_kv_v2(
        self,
        mount_path: str,
        namespace: str | None = None,
    ) -> None:
        """Enable the KV v2 secrets engine at the given mount path.

        If the mount already exists, the operation is silently ignored
        (HTTP 400 with ``"already in use"`` is treated as a no-op).

        Args:
            mount_path: Mount path (e.g. ``config``).
            namespace: Optional namespace to operate in.

        Raises:
            OpenBaoConnectionError: On network errors.
            OpenBaoAuthError: On 401/403.
        """
        if self._http is None:
            raise OpenBaoConnectionError(
                "HTTP client not initialised; use 'async with'",
            )

        path = f"sys/mounts/{mount_path}"
        try:
            resp = await self._http.post(
                f"/v1/{path}",
                headers=self._headers(namespace),
                json={"type": "kv-v2"},
            )
        except httpx.ConnectError as e:
            raise OpenBaoConnectionError(
                f"Cannot connect to OpenBao at {self.addr}: {e}",
            ) from e
        except httpx.TimeoutException as e:
            raise OpenBaoConnectionError(
                f"Timeout connecting to OpenBao at {self.addr}: {e}",
            ) from e

        # 204 = success (created or already exists, idempotent).
        if resp.status_code == 204:
            logger.info("KV v2 mount %r ready in namespace %r (204)", mount_path, namespace)
            return
        # 400 with "already in use" is expected during initialisation.
        if resp.status_code == 400:
            logger.info(
                "KV v2 mount %r already enabled in namespace %r — ignored",
                mount_path,
                namespace,
            )
            return

        self._raise_on_error(resp, path)

    # ═════════════════════════════════════════════════════════════════════════
    # System configuration
    # ═════════════════════════════════════════════════════════════════════════

    async def read_system_config(self) -> dict[str, Any]:
        """Read system config from the combined secret at ``config/data/system``.

        The shell bootstrap scripts write all config keys as a single flat
        secret at this path. This method reads that combined secret directly.

        Returns:
            A flat dict of config key/value pairs, or an empty dict if no
            system secret exists yet.
        """
        try:
            return await self._kv_read(
                f"{KV_MOUNT}/data/system",
                namespace=self._namespace,
            )
        except OpenBaoSecretNotFoundError:
            return {}

    async def write_system_config(self, config: dict[str, Any]) -> None:
        """Write system config as a single combined secret at ``config/data/system``.

        Uses CAS-aware read-modify-write so that concurrent writers (e.g. the
        shell bootstrap script and Python runtime) do not silently clobber each
        other's keys.

        Args:
            config: Flat dict of key/value pairs to merge into the system secret.

        Raises:
            OpenBaoError: If the underlying secret version has changed since
                the read (CAS mismatch).  The caller should retry.
        """
        try:
            existing, version = await self._kv_read(
                f"{KV_MOUNT}/data/system",
                namespace=self._namespace,
                include_meta=True,
            )
        except OpenBaoSecretNotFoundError:
            existing = {}
            version = 0
        existing.update(config)
        await self._kv_write(
            f"{KV_MOUNT}/data/system",
            existing,
            namespace=self._namespace,
            cas_version=version,
        )

    # ═════════════════════════════════════════════════════════════════════════
    # Organisation configuration
    # ═════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _org_ns(org_id: UUID) -> str:
        """Build the namespace path for an organisation.

        Args:
            org_id: Organisation UUID.

        Returns:
            Namespace string (e.g. ``org_<uuid>/``).
        """
        return f"{ORG_NAMESPACE_PREFIX}{org_id}/"

    async def read_org_config(self, org_id: UUID) -> dict[str, Any]:
        """Read all configuration keys for an organisation.

        Same pattern as :meth:`read_system_config` but scoped to the
        organisation's own namespace.

        Args:
            org_id: Organisation UUID.

        Returns:
            Flat dict of key/value pairs, or empty dict if no config
            exists yet.
        """
        ns = self._org_ns(org_id)
        try:
            keys = await self._kv_list(f"{KV_MOUNT}/metadata/", namespace=ns)
        except OpenBaoSecretNotFoundError:
            return {}

        result: dict[str, Any] = {}
        for key in keys:
            try:
                data = await self._kv_read(f"{KV_MOUNT}/data/{key}", namespace=ns)
                result[key] = data.get("value")
            except OpenBaoSecretNotFoundError:
                continue

        return result

    async def write_org_config(
        self,
        org_id: UUID,
        config: dict[str, Any],
    ) -> None:
        """Write organisation-level configuration key/value pairs.

        If a value is ``None``, the corresponding secret is deleted
        rather than written.

        Args:
            org_id: Organisation UUID.
            config: Flat dict of key/value pairs.  ``None`` values
                trigger deletion.
        """
        ns = self._org_ns(org_id)
        # Ensure namespace + KV engine exist (idempotent, one-time cost per org)
        await self.create_org_namespace(org_id)
        for key, value in config.items():
            path = f"{KV_MOUNT}/data/{key}"
            if value is None:
                await self._kv_delete(path, namespace=ns)
            else:
                await self._kv_write(path, {"value": value}, namespace=ns)

    async def create_org_namespace(self, org_id: UUID) -> None:
        """Bootstrap a namespace for a new organisation.

        Creates the ``org_<uuid>`` namespace and enables a KV v2 secrets
        engine at ``config/`` inside it.

        Both operations are idempotent — if the namespace or mount
        already exist, they are silently skipped.

        Args:
            org_id: Organisation UUID.
        """
        ns_name = f"{ORG_NAMESPACE_PREFIX}{org_id}"
        await self.create_namespace(ns_name)
        await self.enable_kv_v2(KV_MOUNT, namespace=ns_name)
        logger.debug("org namespace ensured: %s", ns_name)

    async def delete_org_namespace(self, org_id: UUID) -> None:
        """Tear down the namespace for an organisation.

        Deletes the ``org_<uuid>`` namespace and all secrets within it.

        Args:
            org_id: Organisation UUID.
        """
        ns_name = f"{ORG_NAMESPACE_PREFIX}{org_id}"
        await self.delete_namespace(ns_name)

    # ═════════════════════════════════════════════════════════════════════════
    # Transit engine (encryption-as-a-service)
    # ═════════════════════════════════════════════════════════════════════════

    async def enable_transit_engine(self, mount_path: str = "transit") -> None:
        """Enable the Transit secrets engine at the given mount path.

        If the mount already exists, the operation is silently ignored
        (HTTP 400 with "already in use" is treated as a no-op).

        Args:
            mount_path: Mount path for the transit engine (default ``transit``).

        Raises:
            OpenBaoConnectionError: On network errors.
            OpenBaoAuthError: On 401/403.
        """
        if self._http is None:
            raise OpenBaoConnectionError(
                "HTTP client not initialised; use 'async with'",
            )

        path = f"sys/mounts/{mount_path}"
        try:
            resp = await self._http.post(
                f"/v1/{path}",
                headers=self._headers(),
                json={"type": "transit"},
            )
        except httpx.ConnectError as e:
            raise OpenBaoConnectionError(
                f"Cannot connect to OpenBao: {e}",
            ) from e
        except httpx.TimeoutException as e:
            raise OpenBaoConnectionError(
                f"Timeout connecting to OpenBao: {e}",
            ) from e

        # 204 = success (created or already exists, idempotent).
        # 400 = mount was rejected (e.g. name conflict).
        if resp.status_code == 204:
            logger.info("Transit engine ready at %r (204)", mount_path)
            return
        if resp.status_code == 400:
            logger.info("Transit engine already mounted at %r — ignored", mount_path)
            return

        self._raise_on_error(resp, path)

    async def create_encryption_key(
        self,
        key_name: str,
        key_type: str = "aes256-gcm96",
        mount_path: str = "transit",
    ) -> None:
        """Create an encryption key in the Transit engine.

        If the key already exists, the operation is silently ignored.

        Args:
            key_name: Name of the encryption key.
            key_type: Key type (default ``aes256-gcm96``).  Common options:
                ``aes128-gcm96``, ``aes256-gcm96``, ``chacha20-poly1305``,
                ``ed25519``, ``ecdsa-p256``.
            mount_path: Transit engine mount path.

        Raises:
            OpenBaoConnectionError: On network errors.
            OpenBaoAuthError: On 401/403.
        """
        path = f"{mount_path}/keys/{key_name}"
        try:
            await self._request(
                "POST",
                path,
                json={"type": key_type, "exportable": False, "allow_plaintext_backup": False},
            )
        except OpenBaoError as e:
            # If key already exists, OpenBao returns an error — treat as no-op.
            if "already exists" in str(e).lower():
                logger.info("Encryption key %r already exists — ignored", key_name)
                return
            raise

    async def encrypt_data(
        self,
        key_name: str,
        plaintext: str,
        mount_path: str = "transit",
        context: str | None = None,
    ) -> str:
        """Encrypt plaintext using the named Transit encryption key.

        Args:
            key_name: Name of the encryption key.
            plaintext: Data to encrypt.
            mount_path: Transit engine mount path.
            context: Optional additional authenticated data (AAD) —
                passed as-is to OpenBao, which expects it to be already
                base64-encoded.  This method does NOT encode it so that
                callers can control the encoding (or pass binary context
                that is already base64).

        Returns:
            OpenBao ciphertext string (includes key version info).

        Raises:
            OpenBaoSecretNotFoundError: If the key does not exist.
            OpenBaoError: On encryption failure.
        """
        import base64

        payload: dict[str, Any] = {
            "plaintext": base64.b64encode(plaintext.encode()).decode(),
        }
        if context:
            payload["context"] = base64.b64encode(context.encode()).decode()

        resp = await self._request(
            "POST",
            f"{mount_path}/encrypt/{key_name}",
            json=payload,
        )
        body = resp.json()
        return body["data"]["ciphertext"]

    async def decrypt_data(
        self,
        key_name: str,
        ciphertext: str,
        mount_path: str = "transit",
        context: str | None = None,
    ) -> str:
        """Decrypt ciphertext using the named Transit encryption key.

        Args:
            key_name: Name of the encryption key.
            ciphertext: Ciphertext as returned by :meth:`encrypt_data`.
            mount_path: Transit engine mount path.
            context: Optional AAD that was used during encryption —
                will be base64-encoded before sending to OpenBao.

        Returns:
            Decrypted plaintext string.

        Raises:
            OpenBaoSecretNotFoundError: If the key does not exist.
            OpenBaoError: On decryption failure.
        """
        import base64

        payload: dict[str, Any] = {"ciphertext": ciphertext}
        if context:
            payload["context"] = base64.b64encode(context.encode()).decode()

        resp = await self._request(
            "POST",
            f"{mount_path}/decrypt/{key_name}",
            json=payload,
        )
        body = resp.json()
        return base64.b64decode(body["data"]["plaintext"]).decode()

    async def rotate_encryption_key(
        self,
        key_name: str,
        mount_path: str = "transit",
    ) -> None:
        """Rotate the named encryption key.

        New data will be encrypted with the new key version.  Old data can
        still be decrypted (OpenBao keeps previous key versions).

        Args:
            key_name: Name of the encryption key to rotate.
            mount_path: Transit engine mount path.

        Raises:
            OpenBaoSecretNotFoundError: If the key does not exist.
        """
        await self._request("POST", f"{mount_path}/keys/{key_name}/rotate")

    async def rewrap_data(
        self,
        key_name: str,
        ciphertext: str,
        mount_path: str = "transit",
        context: str | None = None,
    ) -> str:
        """Rewrap ciphertext under the latest version of the encryption key.

        Decrypts then re-encrypts the data without revealing the plaintext
        to the caller (server-side rewrap).  Useful for key rotation.

        Args:
            key_name: Name of the encryption key.
            ciphertext: Ciphertext to rewrap.
            mount_path: Transit engine mount path.
            context: Optional AAD that was used during encryption.

        Returns:
            Ciphertext encrypted with the latest key version.
        """
        import base64

        payload: dict[str, Any] = {"ciphertext": ciphertext}
        if context:
            payload["context"] = base64.b64encode(context.encode()).decode()

        resp = await self._request(
            "POST",
            f"{mount_path}/rewrap/{key_name}",
            json=payload,
        )
        body = resp.json()
        return body["data"]["ciphertext"]
