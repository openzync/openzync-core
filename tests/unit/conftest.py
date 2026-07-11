"""Unit test configuration — OpenBao-aware settings singleton.

Every unit test that exercises code calling ``get_settings()`` needs the
:class:`core.config.Settings` singleton to be initialised beforehand.

This conftest provides a **function-scoped** autouse fixture that sets up
the singleton with dummy values before each test.  Tests that explicitly
test singleton behaviour (``test_config.py::TestSettingsSingleton``) may
override this via their own ``autouse`` fixture that resets ``_settings``.
"""

from __future__ import annotations

import pytest

from core.config import Settings, set_settings


@pytest.fixture(autouse=True)
def _init_settings() -> None:
    """Initialise the ``Settings`` singleton with dummy values.

    This runs before **every** unit test so any code path that calls
    ``get_settings()`` during import or instantiation will succeed.
    Tests that need to verify "not-yet-initialised" behaviour must
    override this fixture (e.g. by resetting ``core.config._settings``
    in their own ``autouse`` fixture).
    """
    import core.config as _cfg

    # The singleton is commonly reset by other autouse fixtures (e.g.
    # test_config.py's TestSettingsSingleton._reset_singleton).  That's
    # fine — this fixture re-initialises it before the next test.
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
        REDIS_URL="redis://localhost:6379/1",
        SECRET_KEY="a" * 32,
        WEBHOOK_SIGNING_SECRET="b" * 32,
        ENVIRONMENT="test",
    )
    set_settings(settings)
