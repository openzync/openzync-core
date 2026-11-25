"""Shared network helpers — proxy trust for client-IP extraction.

Lives in ``core/`` so both the rate-limit middleware and the auth router
import one canonical implementation instead of a private cross-module
import. Fail-closed: unparseable entries and unknown peers are untrusted.
"""

from __future__ import annotations


def is_trusted_proxy(peer_ip: str, trusted: str) -> bool:
    """Return whether a peer IP is in the trusted-proxy list.

    Supports exact addresses and CIDR ranges
    (``"127.0.0.1, 10.0.0.0/8"``). Unparseable entries fail closed
    (treated as untrusted).

    Args:
        peer_ip: The direct ASGI peer address.
        trusted: Comma-separated trusted addresses/CIDRs from settings.

    Returns:
        ``True`` when the peer is trusted to set ``X-Forwarded-For``.
    """
    import ipaddress

    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    for entry in trusted.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if peer in ipaddress.ip_network(entry, strict=False):
                    return True
            elif peer == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False
