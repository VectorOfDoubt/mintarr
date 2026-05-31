"""Sensor registry for the TidalHires quality stack.

This is intentionally small for now: it documents and validates the current
required sensors before optional tools such as CTDB or AcoustID are added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SensorClass = Literal[
    "hard_gate",
    "spectral_heuristic",
    "source_specific_proof",
    "metadata_identity",
    "provenance",
    "library_state",
]
SensorStage = Literal["technical_gate", "spectral", "source_specific", "metadata", "provenance"]
FailPolicy = Literal["block", "review", "skip", "warn"]
Mode = Literal["dry_run", "import"]


@dataclass(frozen=True)
class SensorDefinition:
    name: str
    sensor_class: SensorClass
    stage: SensorStage
    enabled: bool
    required: bool
    timeout_sec: int
    fail_policy: FailPolicy
    applies_to: tuple[str, ...]
    evidence_schema_version: str = "sensor-result-v1"


DEFAULT_SENSORS: tuple[SensorDefinition, ...] = (
    SensorDefinition(
        name="ffprobe",
        sensor_class="hard_gate",
        stage="technical_gate",
        enabled=True,
        required=True,
        timeout_sec=30,
        fail_policy="block",
        applies_to=("tidal", "web", "usenet", "torrent", "soulseek", "cd_rip"),
    ),
    SensorDefinition(
        name="flac_t",
        sensor_class="hard_gate",
        stage="technical_gate",
        enabled=True,
        required=True,
        timeout_sec=120,
        fail_policy="block",
        applies_to=("tidal", "web", "usenet", "torrent", "soulseek", "cd_rip"),
    ),
    SensorDefinition(
        name="flac_detective",
        sensor_class="spectral_heuristic",
        stage="spectral",
        enabled=True,
        required=True,
        timeout_sec=900,
        fail_policy="block",
        applies_to=("tidal", "web", "usenet", "torrent", "soulseek"),
    ),
)


class SensorRegistry:
    def __init__(self, sensors: tuple[SensorDefinition, ...] = DEFAULT_SENSORS):
        self._sensors = {sensor.name: sensor for sensor in sensors}

    def get(self, name: str) -> SensorDefinition:
        return self._sensors[name]

    def ordered(self, *, source_lane: str = "tidal") -> list[SensorDefinition]:
        stage_order = {
            "technical_gate": 0,
            "spectral": 1,
            "source_specific": 2,
            "metadata": 3,
            "provenance": 4,
        }
        return sorted(
            [
                sensor for sensor in self._sensors.values()
                if sensor.enabled and source_lane in sensor.applies_to
            ],
            key=lambda sensor: (stage_order[sensor.stage], sensor.name),
        )

    def validate_mode(self, *, mode: Mode = "import") -> None:
        if mode != "import":
            return
        disabled_required = [
            sensor.name for sensor in self._sensors.values()
            if sensor.required and not sensor.enabled
        ]
        if disabled_required:
            names = ", ".join(sorted(disabled_required))
            raise ValueError(f"required sensors cannot be disabled in import mode: {names}")


default_registry = SensorRegistry()
