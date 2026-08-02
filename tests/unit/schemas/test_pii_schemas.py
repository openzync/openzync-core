"""Unit tests for PII-related Pydantic schemas — enum, config, and stats.

Covers PIIMode, PIIConfig (constraints, defaults, custom), and PIIStats.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.pii import PIIConfig, PIIMode, PIIStats


@pytest.mark.unit
class TestPIIModeEnum:
    """PIIMode enum values and string access."""

    def test_off_value(self) -> None:
        """PIIMode.OFF has value 'off'."""
        assert PIIMode.OFF.value == "off"

    def test_mask_value(self) -> None:
        """PIIMode.MASK has value 'mask'."""
        assert PIIMode.MASK.value == "mask"

    def test_block_value(self) -> None:
        """PIIMode.BLOCK has value 'block'."""
        assert PIIMode.BLOCK.value == "block"

    def test_all_members_covered(self) -> None:
        """All expected enum members exist."""
        members = {m.value for m in PIIMode}
        assert members == {"off", "mask", "block"}

    def test_string_comparison(self) -> None:
        """PIIMode values compare equal to their string values."""
        assert PIIMode("off") is PIIMode.OFF
        assert PIIMode("mask") is PIIMode.MASK
        assert PIIMode("block") is PIIMode.BLOCK

    def test_invalid_value_raises_value_error(self) -> None:
        """Constructing PIIMode from an invalid string raises ValueError."""
        with pytest.raises(ValueError, match="'nope' is not a valid PIIMode"):
            PIIMode("nope")


@pytest.mark.unit
class TestPIIConfigSchema:
    """PIIConfig validation — defaults, custom values, and field constraints."""

    def test_defaults(self) -> None:
        """PIIConfig with no arguments uses all defaults."""
        config = PIIConfig()

        assert config.mode is PIIMode.OFF
        assert config.min_confidence == 0.7
        assert config.sensitivity == "medium"
        assert config.enabled_types == [
            "email",
            "phone",
            "ssn",
            "credit_card",
            "ip_address",
            "api_key",
        ]

    def test_default_enabled_types_is_fresh_list_per_instance(self) -> None:
        """Each PIIConfig gets its own enabled_types list (no shared mutable default)."""
        config_a = PIIConfig()
        config_b = PIIConfig()

        config_a.enabled_types.append("extra_type")
        assert "extra_type" not in config_b.enabled_types
        assert len(config_b.enabled_types) == 6

    def test_custom_mode(self) -> None:
        """PIIConfig accepts a custom mode."""
        config = PIIConfig(mode=PIIMode.MASK)
        assert config.mode is PIIMode.MASK

    def test_custom_enabled_types(self) -> None:
        """PIIConfig accepts a custom enabled_types list."""
        config = PIIConfig(enabled_types=["email", "ssn"])
        assert config.enabled_types == ["email", "ssn"]

    def test_custom_min_confidence(self) -> None:
        """PIIConfig accepts a custom min_confidence."""
        config = PIIConfig(min_confidence=0.5)
        assert config.min_confidence == 0.5

    def test_min_confidence_zero_is_valid(self) -> None:
        """min_confidence=0.0 is at the lower bound and accepted."""
        config = PIIConfig(min_confidence=0.0)
        assert config.min_confidence == 0.0

    def test_min_confidence_one_is_valid(self) -> None:
        """min_confidence=1.0 is at the upper bound and accepted."""
        config = PIIConfig(min_confidence=1.0)
        assert config.min_confidence == 1.0

    def test_min_confidence_below_zero_raises_validation_error(self) -> None:
        """min_confidence below 0.0 fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            PIIConfig(min_confidence=-0.1)

        errors = exc_info.value.errors()
        assert any("min_confidence" in e["loc"] for e in errors)

    def test_min_confidence_above_one_raises_validation_error(self) -> None:
        """min_confidence above 1.0 fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            PIIConfig(min_confidence=1.1)

        errors = exc_info.value.errors()
        assert any("min_confidence" in e["loc"] for e in errors)

    def test_custom_sensitivity(self) -> None:
        """PIIConfig accepts valid sensitivity values."""
        for level in ("low", "medium", "high"):
            config = PIIConfig(sensitivity=level)
            assert config.sensitivity == level

    def test_invalid_sensitivity_raises_validation_error(self) -> None:
        """Sensitivity value not matching the regex pattern fails validation."""
        with pytest.raises(ValidationError) as exc_info:
            PIIConfig(sensitivity="very_high")

        errors = exc_info.value.errors()
        assert any("sensitivity" in e["loc"] for e in errors)

    def test_all_fields_set(self) -> None:
        """PIIConfig with all fields explicitly set."""
        config = PIIConfig(
            mode=PIIMode.BLOCK,
            enabled_types=["email", "phone"],
            min_confidence=0.85,
            sensitivity="high",
        )

        assert config.mode is PIIMode.BLOCK
        assert config.enabled_types == ["email", "phone"]
        assert config.min_confidence == 0.85
        assert config.sensitivity == "high"

    def test_round_trip_serialization(self) -> None:
        """PIIConfig serializes to dict and back without loss."""
        original = PIIConfig(
            mode=PIIMode.MASK,
            enabled_types=["email", "credit_card"],
            min_confidence=0.9,
            sensitivity="low",
        )

        data = original.model_dump()
        assert data["mode"] == "mask"
        assert data["enabled_types"] == ["email", "credit_card"]
        assert data["min_confidence"] == 0.9
        assert data["sensitivity"] == "low"

        restored = PIIConfig.model_validate(data)
        assert restored.mode is PIIMode.MASK
        assert restored.enabled_types == ["email", "credit_card"]
        assert restored.min_confidence == 0.9
        assert restored.sensitivity == "low"

    def test_enabled_types_empty_list(self) -> None:
        """An empty enabled_types list is valid (no PII types scanned)."""
        config = PIIConfig(enabled_types=[])
        assert config.enabled_types == []


@pytest.mark.unit
class TestPIIStatsSchema:
    """PIIStats validation — defaults and custom values."""

    def test_defaults(self) -> None:
        """PIIStats with no arguments uses all defaults."""
        stats = PIIStats()

        assert stats.detections_count == 0
        assert stats.types_found == []
        assert stats.action_taken == "none"
        assert stats.duration_ms == 0.0

    def test_custom_values(self) -> None:
        """PIIStats accepts explicitly set values."""
        stats = PIIStats(
            detections_count=3,
            types_found=["email", "phone"],
            action_taken="masked",
            duration_ms=12.5,
        )

        assert stats.detections_count == 3
        assert stats.types_found == ["email", "phone"]
        assert stats.action_taken == "masked"
        assert stats.duration_ms == 12.5

    def test_zero_detections(self) -> None:
        """PIIStats with zero detections and empty types."""
        stats = PIIStats(
            detections_count=0,
            types_found=[],
            action_taken="none",
            duration_ms=0.0,
        )

        assert stats.detections_count == 0
        assert stats.types_found == []
        assert stats.action_taken == "none"
        assert stats.duration_ms == 0.0

    def test_negative_detections_count(self) -> None:
        """A negative detections_count is schema-valid (business logic enforces non-negative)."""
        # Pydantic int fields with no constraints accept negative values
        stats = PIIStats(detections_count=-1)
        assert stats.detections_count == -1

    def test_blocked_action(self) -> None:
        """PIIStats with action_taken='blocked' is accepted."""
        stats = PIIStats(action_taken="blocked")
        assert stats.action_taken == "blocked"

    def test_float_duration_ms(self) -> None:
        """duration_ms accepts float values."""
        stats = PIIStats(duration_ms=0.5)
        assert stats.duration_ms == 0.5

    def test_round_trip_serialization(self) -> None:
        """PIIStats serializes to dict and back without loss."""
        original = PIIStats(
            detections_count=2,
            types_found=["ssn", "credit_card"],
            action_taken="masked",
            duration_ms=8.25,
        )

        data = original.model_dump()
        assert data["detections_count"] == 2
        assert data["types_found"] == ["ssn", "credit_card"]
        assert data["action_taken"] == "masked"
        assert data["duration_ms"] == 8.25

        restored = PIIStats.model_validate(data)
        assert restored.detections_count == 2
        assert restored.types_found == ["ssn", "credit_card"]
        assert restored.action_taken == "masked"
        assert restored.duration_ms == 8.25
