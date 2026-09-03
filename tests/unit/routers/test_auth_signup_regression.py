"""Regression placeholder — pre-existing signup 500 (uuid:"" bug).

KNOWN FAILURE — do not assert success.  ``POST /v1/auth/signup`` currently
returns ``500 "Internal Server Error"`` (text) with::

    sqlalchemy.exc.InvalidTextRepresentationError:
        invalid input syntax for type uuid: ""

raised by ``SELECT users WHERE email``.  This pre-existing auth bug blocks
signup E2E.  No auth code was changed for this placeholder.

When the auth bug is fixed, replace the skip below with a real regression
test asserting the fixed behavior (201 + OTP sent), and delete this module
docstring's KNOWN FAILURE notice.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.skip(
    reason=(
        "KNOWN FAILURE: POST /v1/auth/signup → 500 "
        'InvalidTextRepresentationError: invalid input syntax for type uuid: "" '
        "(SELECT users WHERE email). Convert to a success-path regression "
        "test once the pre-existing auth bug is fixed."
    )
)
def test_signup_e2e_placeholder() -> None:
    """Placeholder — asserts nothing until the signup uuid:"" bug is fixed."""
