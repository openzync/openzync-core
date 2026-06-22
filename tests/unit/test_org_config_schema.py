"""Unit tests for ``schemas.organization_config``.

Tests the Pydantic schema resolution logic independently of any DB or cache.
"""

from __future__ import annotations

from typing import Any

import pytest

from schemas.organization_config import (
    OrgConfigBase,
    OrgConfigResponse,
    UpdateOrgConfigRequest,
)


class TestOrgConfigBase:
    """Validate the raw-DB-shape schema."""

    def test_defaults_are_none(self) -> None:
        """Every field in OrgConfigBase should default to None,
        except ``graph_backend`` which defaults to ``"surrealdb"``.
        """
        cfg = OrgConfigBase()
        for field_name in OrgConfigBase.model_fields:
            if field_name == "graph_backend":
                assert getattr(cfg, field_name) == "surrealdb"
                continue
            assert getattr(cfg, field_name) is None, (
                f"Expected {field_name} to be None, got {getattr(cfg, field_name)!r}"
            )

    def test_extra_fields_ignored(self) -> None:
        """Unknown keys should be silently dropped (extra='ignore')."""
        cfg = OrgConfigBase.model_validate(
            {"llm_backend": "ollama", "nonexistent": "should_be_ignored"}
        )
        assert cfg.llm_backend == "ollama"
        with pytest.raises(AttributeError):
            _ = cfg.nonexistent  # type: ignore[attr-defined]

    def test_partial_construction(self) -> None:
        """Constructing with a subset of fields should work."""
        cfg = OrgConfigBase(llm_backend="openai", embedding_dim=1536)
        assert cfg.llm_backend == "openai"
        assert cfg.embedding_dim == 1536
        assert cfg.llm_model is None  # not set
        assert cfg.graph_backend == "surrealdb"  # default when not provided


class TestOrgConfigBaseToDict:
    """Validate the helper methods on OrgConfigBase."""

    def test_to_llm_config_dict_keys(self) -> None:
        """to_llm_config_dict() should produce the expected provider keys."""
        cfg = OrgConfigBase(
            llm_backend="openai",
            llm_model="gpt-4o",
            openai_api_key="sk-test",
            ollama_base_url="http://ollama:11434",
            llm_temperature=0.7,
            llm_max_tokens=2048,
        )
        d = cfg.to_llm_config_dict()
        assert d["llm_backend"] == "openai"
        assert d["openai_api_key"] == "sk-test"
        assert d["openai_model"] == "gpt-4o"
        assert d["ollama_base_url"] == "http://ollama:11434"
        # Provider-specific model keys
        assert d["anthropic_model"] == "gpt-4o"
        assert d["azure_deployment"] == "gpt-4o"
        assert d["model"] == "gpt-4o"
        # Temperature and max_tokens
        assert d["temperature"] == 0.7
        assert d["max_tokens"] == 2048

    def test_to_llm_config_dict_excludes_none(self) -> None:
        """Fields with None values should be omitted from the dict."""
        cfg = OrgConfigBase()
        d = cfg.to_llm_config_dict()
        assert d == {}

    def test_to_llm_config_dict_temperature_boundaries(self) -> None:
        """Temperature should respect inclusive bounds."""
        cfg = OrgConfigBase(llm_temperature=0.0)
        assert cfg.to_llm_config_dict()["temperature"] == 0.0
        cfg = OrgConfigBase(llm_temperature=2.0)
        assert cfg.to_llm_config_dict()["temperature"] == 2.0

    def test_to_embedding_config_dict(self) -> None:
        """to_embedding_config_dict() should return flat embedding fields."""
        cfg = OrgConfigBase(
            embedding_backend="ollama",
            embedding_model="nomic-embed-text",
            embedding_dim=768,
        )
        d = cfg.to_embedding_config_dict()
        assert d["embedding_backend"] == "ollama"
        assert d["embedding_model"] == "nomic-embed-text"
        assert d["embedding_dim"] == 768

    def test_to_embedding_config_dict_excludes_none(self) -> None:
        """Fields with None values should be omitted from the dict."""
        cfg = OrgConfigBase()
        d = cfg.to_embedding_config_dict()
        assert d == {}


class TestUpdateOrgConfigRequest:
    """Validate the partial-update request schema."""

    def test_defaults_are_none(self) -> None:
        """Every field should default to None for true partial updates."""
        req = UpdateOrgConfigRequest()
        for field_name in UpdateOrgConfigRequest.model_fields:
            assert getattr(req, field_name) is None, (
                f"Expected {field_name} to be None, got {getattr(req, field_name)!r}"
            )

    def test_only_unset_fields_excluded(self) -> None:
        """model_dump(exclude_unset=True) should only include provided fields."""
        req = UpdateOrgConfigRequest(llm_backend="anthropic")
        dumped = req.model_dump(exclude_unset=True)
        assert dumped == {"llm_backend": "anthropic"}

    def test_null_explicitly_sets_none(self) -> None:
        """Setting a field to None explicitly should be included in dump."""
        req = UpdateOrgConfigRequest(llm_backend=None)
        dumped = req.model_dump(exclude_unset=True)
        assert dumped == {"llm_backend": None}

    def test_embedding_dim_validation(self) -> None:
        """embedding_dim must be between 64 and 4096."""
        with pytest.raises(Exception, match="Input should be greater than or equal to 64"):
            UpdateOrgConfigRequest(embedding_dim=16)

        with pytest.raises(Exception, match="Input should be less than or equal to 4096"):
            UpdateOrgConfigRequest(embedding_dim=8192)

    def test_graph_max_traversal_depth_validation(self) -> None:
        """graph_max_traversal_depth must be between 1 and 10."""
        with pytest.raises(Exception, match="Input should be greater than or equal to 1"):
            UpdateOrgConfigRequest(graph_max_traversal_depth=0)

        with pytest.raises(Exception, match="Input should be less than or equal to 10"):
            UpdateOrgConfigRequest(graph_max_traversal_depth=20)


class TestOrgConfigResponse:
    """Validate the response schema."""

    def test_response_contains_stored_config(self) -> None:
        """Response should include the stored config."""
        stored = OrgConfigBase(llm_backend="openai")
        resp = OrgConfigResponse(stored=stored)
        assert resp.stored.llm_backend == "openai"

    def test_response_stored_all_none_when_empty(self) -> None:
        """Response with empty stored config should have all-None fields,
        except ``graph_backend`` which defaults to ``"surrealdb"``.
        """
        resp = OrgConfigResponse(stored=OrgConfigBase())
        dumped = resp.stored.model_dump()
        for field_name, value in dumped.items():
            if field_name == "graph_backend":
                assert value == "surrealdb"
            else:
                assert value is None, f"Expected {field_name} to be None, got {value!r}"
