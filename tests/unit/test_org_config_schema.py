"""Unit tests for ``schemas.platform_config``.

Tests the Pydantic schema resolution logic independently of any DB or cache.
"""

from __future__ import annotations

import pytest

from schemas.platform_config import (
    PlatformConfigBase,
    PlatformConfigResponse,
    UpdatePlatformConfigRequest,
)


class TestPlatformConfigBase:
    """Validate the raw-DB-shape schema."""

    def test_defaults_are_none(self) -> None:
        """Every field in PlatformConfigBase should default to None,
        except ``graph_backend`` which defaults to ``"falkordb"``.
        """
        cfg = PlatformConfigBase()
        for field_name in PlatformConfigBase.model_fields:
            if field_name == "graph_backend":
                assert getattr(cfg, field_name) == "falkordb"
                continue
            assert getattr(cfg, field_name) is None, (
                f"Expected {field_name} to be None, got {getattr(cfg, field_name)!r}"
            )

    def test_extra_fields_ignored(self) -> None:
        """Unknown keys should be silently dropped (extra='ignore')."""
        cfg = PlatformConfigBase.model_validate(
            {"llm_backend": "ollama", "llm_base_url": "http://localhost:11434", "nonexistent": "should_be_ignored"}
        )
        assert cfg.llm_backend == "ollama"
        with pytest.raises(AttributeError):
            _ = cfg.nonexistent  # type: ignore[attr-defined]

    def test_partial_construction(self) -> None:
        """Constructing with a subset of fields should work."""
        cfg = PlatformConfigBase(llm_backend="openai", llm_model="gpt-4o-mini", llm_api_key="sk-test")
        assert cfg.llm_backend == "openai"
        assert cfg.llm_model == "gpt-4o-mini"
        assert cfg.graph_search_type is None  # not set
        assert cfg.graph_backend == "falkordb"  # default when not provided


class TestPlatformConfigBaseToDict:
    """Validate the helper methods on PlatformConfigBase."""

    def test_to_llm_config_keys(self) -> None:
        """to_llm_config() should produce the unified provider config."""
        cfg = PlatformConfigBase(
            llm_backend="openai",
            llm_model="gpt-4o",
            llm_api_key="sk-test",
            llm_temperature=0.7,
            llm_max_tokens=2048,
        )
        c = cfg.to_llm_config()
        assert c.provider == "openai"
        assert c.model == "gpt-4o"
        assert c.api_key == "sk-test"
        # Temperature and max_tokens
        assert c.temperature == 0.7
        assert c.max_tokens == 2048

    def test_to_llm_config_requires_backend(self) -> None:
        """No backend means no LLM config — raises instead of an empty dict."""
        from core.exceptions import LLMConfigurationError

        with pytest.raises(LLMConfigurationError):
            PlatformConfigBase().to_llm_config()

    def test_to_llm_config_ignores_prompt_caching(self) -> None:
        """prompt_caching overrides stay on the stored schema, not the call config."""
        cfg = PlatformConfigBase(
            llm_backend="openai",
            llm_api_key="sk-test",
            prompt_caching={
                "enabled": False,
                "anthropic_min_tokens": 4096,
            },
        )
        assert cfg.prompt_caching is not None
        assert cfg.prompt_caching.enabled is False
        assert cfg.prompt_caching.anthropic_min_tokens == 4096
        assert not hasattr(cfg.to_llm_config(), "prompt_caching")

    def test_to_llm_config_prompt_caching_excludes_none(self) -> None:
        """Null prompt_caching fields are omitted, not leaked as None."""
        cfg = PlatformConfigBase(
            llm_backend="openai",
            llm_api_key="sk-test",
            prompt_caching={
                "enabled": None,
                "anthropic_min_tokens": 4096,
            },
        )
        assert cfg.model_dump(exclude_none=True)["prompt_caching"] == {
            "anthropic_min_tokens": 4096
        }

    def test_to_llm_config_temperature_boundaries(self) -> None:
        """Temperature should respect inclusive bounds."""
        cfg = PlatformConfigBase(
            llm_backend="openai", llm_api_key="sk-test", llm_temperature=0.0
        )
        assert cfg.to_llm_config().temperature == 0.0
        cfg = PlatformConfigBase(
            llm_backend="openai", llm_api_key="sk-test", llm_temperature=2.0
        )
        assert cfg.to_llm_config().temperature == 2.0


class TestUpdatePlatformConfigRequest:
    """Validate the partial-update request schema."""

    def test_defaults_are_none(self) -> None:
        """Every field should default to None for true partial updates."""
        req = UpdatePlatformConfigRequest()
        for field_name in UpdatePlatformConfigRequest.model_fields:
            assert getattr(req, field_name) is None, (
                f"Expected {field_name} to be None, got {getattr(req, field_name)!r}"
            )

    def test_only_unset_fields_excluded(self) -> None:
        """model_dump(exclude_unset=True) should only include provided fields."""
        req = UpdatePlatformConfigRequest(llm_backend="anthropic", llm_api_key="sk-test")
        dumped = req.model_dump(exclude_unset=True)
        assert dumped == {"llm_backend": "anthropic", "llm_api_key": "sk-test"}

    def test_null_explicitly_sets_none(self) -> None:
        """Setting a field to None explicitly should be included in dump."""
        req = UpdatePlatformConfigRequest(llm_backend=None)
        dumped = req.model_dump(exclude_unset=True)
        assert dumped == {"llm_backend": None}

    def test_graph_max_traversal_depth_validation(self) -> None:
        """graph_max_traversal_depth must be between 1 and 10."""
        with pytest.raises(
            Exception, match="Input should be greater than or equal to 1"
        ):
            UpdatePlatformConfigRequest(graph_max_traversal_depth=0)

        with pytest.raises(Exception, match="Input should be less than or equal to 10"):
            UpdatePlatformConfigRequest(graph_max_traversal_depth=20)

    def test_postgres_graph_backend_hard_rejected(self) -> None:
        """postgres graph_backend is hard-rejected — deprecated, removal v1.1.0."""
        with pytest.raises(Exception, match=r"PostgreSQL graph backend.*v1\.1\.0.*410"):
            UpdatePlatformConfigRequest(graph_backend="postgres")

    def test_falkordb_and_surrealdb_allowed(self) -> None:
        """falkordb and surrealdb graph_backends are still allowed."""
        assert (
            UpdatePlatformConfigRequest(graph_backend="falkordb").graph_backend == "falkordb"
        )
        assert (
            UpdatePlatformConfigRequest(graph_backend="surrealdb").graph_backend
            == "surrealdb"
        )
        assert UpdatePlatformConfigRequest(graph_backend="none").graph_backend == "none"
        assert UpdatePlatformConfigRequest(graph_backend=None).graph_backend is None


class TestPlatformConfigResponse:
    """Validate the response schema."""

    def test_response_contains_stored_config(self) -> None:
        """Response should include the stored config."""
        stored = PlatformConfigBase(llm_backend="openai", llm_api_key="sk-test")
        resp = PlatformConfigResponse(stored=stored)
        assert resp.stored.llm_backend == "openai"

    def test_response_stored_all_none_when_empty(self) -> None:
        """Response with empty stored config should have all-None fields,
        except ``graph_backend`` which defaults to ``"falkordb"``.
        """
        resp = PlatformConfigResponse(stored=PlatformConfigBase())
        dumped = resp.stored.model_dump()
        for field_name, value in dumped.items():
            if field_name == "graph_backend":
                assert value == "falkordb"
            else:
                assert value is None, f"Expected {field_name} to be None, got {value!r}"
