"""Unit test configuration — hermetic Settings singleton.

Every unit test that exercises code calling ``get_settings()`` needs the
:class:`core.config.Settings` singleton to be initialised beforehand.

This conftest provides a **function-scoped** autouse fixture that sets up
the singleton with dummy values before each test.  Tests that explicitly
test singleton behaviour (``test_config.py::TestSettingsSingleton``) may
override this via their own ``autouse`` fixture that resets ``_settings``.

Hermeticity: the repo ``.env`` file (local dev secrets, MailHog SMTP
port, FalkorDB URL, …) must NOT leak into unit tests — ``Settings``
reads ``.env`` by default.  The fixture therefore defeats ``env_file``
and clears ``OZ_*`` vars, then constructs ``Settings`` from explicit
kwargs only.
"""

from __future__ import annotations

import os

import pytest

from core.config import Settings, set_settings


@pytest.fixture(autouse=True)
def _mock_enrichment_config(request: pytest.FixtureRequest) -> object:
    """Stub enrichment resolvers for ingest-path unit tests only.

    ``IngestMixin._resolve_enrichments`` does fresh lazy
    ``from core.{system_config,org_config} import get_*_config`` per call,
    so patching the source-module attributes covers every ingest test
    without touching ``mock_db`` (which must stay a plain repo-level seam).
    Defaults are all-ON (``SystemConfigResponse()`` /
    ``OrgConfigBase()``) — the production default for a fresh platform.

    Scoped to the four ingest-path modules: a global patch would shadow
    the real resolvers for suites that test them directly
    (``core/test_org_config.py`` and ``core/test_system_config.py`` call
    the real ``update_*`` → ``get_*`` round-trip; prompt/worker suites
    assert on real resolver-driven behaviour).
    """
    from unittest.mock import AsyncMock, patch

    from schemas.organization_config import OrgConfigBase
    from schemas.system_config import SystemConfigResponse

    module = getattr(request.node, "module", None)
    name = getattr(module, "__name__", "") or ""
    short = name.rsplit(".", 1)[-1]
    if short not in {
        "test_memory_service",
        "test_ingest_dedup_replay",
        "test_ingest_purge_failure",
        "test_pii_fail_closed",
    }:
        yield
        return

    with (
        patch(
            "core.system_config.get_system_config",
            new=AsyncMock(return_value=SystemConfigResponse()),
        ),
        patch(
            "core.org_config.get_org_config",
            new=AsyncMock(return_value=OrgConfigBase()),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _init_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Initialise the ``Settings`` singleton with dummy values.

    This runs before **every** unit test so any code path that calls
    ``get_settings()`` during import or instantiation will succeed.
    Tests that need to verify "not-yet-initialised" behaviour must
    override this fixture (e.g. by resetting ``core.config._settings``
    in their own ``autouse`` fixture).
    """
    # Defeat .env + real-env leakage — unit tests see explicit kwargs only.
    monkeypatch.setitem(Settings.model_config, "env_file", "/dev/null")
    for var in list(os.environ):
        if var.startswith("OZ_"):
            monkeypatch.delenv(var, raising=False)

    # The singleton is commonly reset by other autouse fixtures (e.g.
    # test_config.py's TestSettingsSingleton._reset_singleton).  That's
    # fine — this fixture re-initialises it before the next test.
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
        REDIS_URL="redis://localhost:6379/1",
        SECRET_KEY="a" * 32,
        WEBHOOK_SIGNING_SECRET="b" * 32,
        ENVIRONMENT="test",
        S3_ENDPOINT_URL="http://minio:9000",
        S3_ACCESS_KEY_ID="test-key",
        S3_SECRET_ACCESS_KEY="test-secret",
    )
    set_settings(settings)
