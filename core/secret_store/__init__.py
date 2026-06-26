"""Secret store dispatcher — selects encryption backend based on configuration.

Usage (app lifespan)::

    from core.secret_store import init_secret_store_dispatcher

    dispatcher = init_secret_store_dispatcher()
    app.state.secret_store = dispatcher

Usage (per-request)::

    backend = request.app.state.secret_store.resolve()
    encrypted = await backend.encrypt(plaintext, context=str(org_id))

The dispatcher follows the same registry pattern as
:class:`~core.graph_backend.GraphBackendDispatcher`.  New backends
(e.g. Vault, AWS KMS) implement :class:`KeyEncryptionBackend`, register
themselves, and are selected via ``MG_SECRET_STORE_BACKEND``.
"""

from __future__ import annotations

import logging

from core.config import settings
from core.secret_store.interface import KeyEncryptionBackend

logger = logging.getLogger(__name__)


class SecretStoreDispatcher:
    """Registry of encryption backend classes + per-app resolution.

    This is an app-level singleton.  It holds the registered **classes**
    and creates a single instance (on first call) because backends are
    stateless and thread-safe.

    Usage::

        dispatcher = SecretStoreDispatcher()
        dispatcher.register("fernet", FernetKeyEncryption)
        dispatcher.register("vault", VaultKeyEncryption)

        # Resolve the configured backend for this deployment
        backend = dispatcher.resolve()
        await backend.encrypt("my-api-key", context=str(org_id))
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[KeyEncryptionBackend]] = {}
        self._instance: KeyEncryptionBackend | None = None
        self._resolved_name: str | None = None

    def register(self, name: str, backend_cls: type[KeyEncryptionBackend]) -> None:
        """Register a backend class under a short name (e.g. ``"fernet"``).

        Args:
            name: Short identifier (e.g. ``"fernet"``, ``"vault"``).
            backend_cls: A class that implements :class:`KeyEncryptionBackend`.
        """
        self._registry[name] = backend_cls
        logger.info("secret_store.backend_registered", extra={"backend": name})

    def resolve(self, backend_name: str | None = None) -> KeyEncryptionBackend:
        """Resolve and create (once) the configured encryption backend.

        The backend instance is created once and cached.  Subsequent calls
        return the same instance (backends are stateless and thread-safe).

        Args:
            backend_name: Override the backend name.  When ``None``, uses
                ``settings.MG_SECRET_STORE_BACKEND``.

        Returns:
            An initialised :class:`KeyEncryptionBackend` instance.

        Raises:
            RuntimeError: If the backend name is not registered or no
                master key is configured.
        """
        name = backend_name or settings.SECRET_STORE_BACKEND

        # Return cached instance if the same backend is requested
        if self._instance is not None and self._resolved_name == name:
            return self._instance

        cls = self._registry.get(name)
        if cls is None:
            raise RuntimeError(
                f"Unknown secret store backend: '{name}'. "
                f"Available backends: {list(self._registry.keys())}. "
                f"Set MG_SECRET_STORE_BACKEND in your environment."
            )

        master_key = settings.MASTER_ENCRYPTION_KEY
        if not master_key:
            raise RuntimeError(
                "MG_MASTER_ENCRYPTION_KEY is not configured. "
                "Set a Fernet-compatible key in your environment before "
                "using the secret store."
            )

        self._instance = cls(master_key=master_key)
        self._resolved_name = name
        logger.info(
            "secret_store.instance_created",
            extra={"backend": name},
        )
        return self._instance


def init_secret_store_dispatcher() -> SecretStoreDispatcher:
    """Create and populate the dispatcher with all registered backends.

    Call once during the application lifespan and store the result in
    ``app.state``::

        from core.secret_store import init_secret_store_dispatcher

        app.state.secret_store = init_secret_store_dispatcher()

    Raises:
        RuntimeError: If ``MG_MASTER_ENCRYPTION_KEY`` is not configured
            (hard fail — no encryption without a key).
    """
    from core.secret_store.fernet import FernetKeyEncryption

    # Hard-fail early if no master key is configured
    if not settings.MASTER_ENCRYPTION_KEY:
        raise RuntimeError(
            "MG_MASTER_ENCRYPTION_KEY is not configured. "
            "The application will not start without a master encryption key. "
            "Generate one with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\""
        )

    dispatcher = SecretStoreDispatcher()
    dispatcher.register("fernet", FernetKeyEncryption)
    return dispatcher
