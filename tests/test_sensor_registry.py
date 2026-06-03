"""Tests for the quality-stack sensor registry."""

from __future__ import annotations

import pytest

from sensor_registry import SensorDefinition, SensorRegistry, default_registry


def test_default_registry_orders_required_current_sensors():
    sensors = default_registry.ordered(source_lane="tidal")

    assert [sensor.name for sensor in sensors] == [
        "ffprobe",
        "flac_t",
        "flac_detective",
    ]
    assert all(sensor.enabled for sensor in sensors)
    assert all(sensor.required for sensor in sensors)
    assert default_registry.get("ffprobe").fail_policy == "block"


def test_registry_filters_by_source_lane():
    sensors = default_registry.ordered(source_lane="cd_rip")

    assert [sensor.name for sensor in sensors] == ["ffprobe", "flac_t"]


def test_registry_rejects_disabled_required_sensor_in_import_mode():
    registry = SensorRegistry(
        (
            SensorDefinition(
                name="ffprobe",
                sensor_class="hard_gate",
                stage="technical_gate",
                enabled=False,
                required=True,
                timeout_sec=30,
                fail_policy="block",
                applies_to=("tidal",),
            ),
        )
    )

    with pytest.raises(ValueError, match="required sensors"):
        registry.validate_mode(mode="import")

    registry.validate_mode(mode="dry_run")
