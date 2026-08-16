"""Unit tests for ``core.llm.build_cache_config``.

The ``Settings`` singleton is pre-initialised by the autouse fixture in
``tests/unit/conftest.py`` with default ``PROMPT_CACHING_*`` values
(enabled=True, min_tokens=1024, ttl="5m").
"""

from __future__ import annotations

from core.llm import PromptCachingConfig, build_cache_config


class TestBuildCacheConfig:
    """Validate per-org override resolution and null handling."""

    def test_absent_key_uses_global_defaults(self) -> None:
        """No ``prompt_caching`` key → global defaults, unchanged behaviour."""
        cfg = build_cache_config(org_config={"llm_backend": "anthropic"})
        assert cfg == PromptCachingConfig(
            enabled=True,
            anthropic_min_tokens=1024,
            anthropic_cache_ttl="5m",
            session_id=None,
        )

    def test_explicit_null_falls_back_to_global_default(self) -> None:
        """``enabled: None`` must NOT disable caching — fall back to global."""
        cfg = build_cache_config(
            org_config={"prompt_caching": {"enabled": None}}
        )
        assert cfg.enabled is True
        assert cfg.anthropic_min_tokens == 1024
        assert cfg.anthropic_cache_ttl == "5m"

    def test_explicit_false_is_honoured(self) -> None:
        """Explicit ``enabled: False`` disables caching for the org."""
        cfg = build_cache_config(
            org_config={"prompt_caching": {"enabled": False}}
        )
        assert cfg.enabled is False

    def test_explicit_values_override_globals(self) -> None:
        """Non-null per-org values override the global defaults."""
        cfg = build_cache_config(
            org_config={
                "prompt_caching": {
                    "enabled": True,
                    "anthropic_min_tokens": 4096,
                    "anthropic_cache_ttl": "1h",
                }
            }
        )
        assert cfg == PromptCachingConfig(
            enabled=True,
            anthropic_min_tokens=4096,
            anthropic_cache_ttl="1h",
            session_id=None,
        )
