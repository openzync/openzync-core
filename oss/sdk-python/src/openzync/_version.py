"""Version information for openzync.

This module is intentionally import-free so it can be imported
anywhere without circular dependency issues.
"""

try:
    from importlib.metadata import version as _metadata_version
    __version__ = _metadata_version("openzync")
except Exception:
    __version__ = "0.0.0"
