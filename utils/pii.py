"""PII helpers for safe logging.

``mask_email`` is the single implementation behind every
``extra={"email": ...}`` log call on auth paths — one definition so the
mask shape cannot drift between services.
"""

from __future__ import annotations


def mask_email(email: str) -> str:
    """Mask an email for logging — first/last char of the local part.

    Args:
        email: The full email address (may be empty/unknown).

    Returns:
        Masked string (e.g. ``u**r@example.com``), safe for logs.
    """
    local, _, domain = email.partition("@")
    if not local:
        return email
    if len(local) <= 1:
        return f"{local}**@{domain}"
    return f"{local[0]}**{local[-1]}@{domain}"
