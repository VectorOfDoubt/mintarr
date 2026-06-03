"""Static connector registry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import (
    ConnectorKind,
    ConnectorMode,
    MANIFEST_API_VERSION,
    manifest_to_dict,
    unix_ts_to_iso,
)
from .config import config_for_manifest, configured_mode

if TYPE_CHECKING:
    from .base import Connector

log = logging.getLogger("tidalhires.connectors.registry")

_connectors: dict[str, "Connector"] = {}


def register(connector: "Connector") -> None:
    connector_id = connector.manifest.id
    if connector_id in _connectors:
        raise ValueError(f"Connector '{connector_id}' already registered")
    _connectors[connector_id] = connector


def get_connector(connector_id: str) -> "Connector | None":
    return _connectors.get(connector_id)


def all_connectors() -> list["Connector"]:
    return list(_connectors.values())


def connector_for_adapter(adapter_name: str) -> "Connector | None":
    for connector in _connectors.values():
        if getattr(connector, "adapter_name", None) == adapter_name:
            return connector
    return None


def required_connectors() -> list["Connector"]:
    return [
        connector for connector in _connectors.values() if connector.manifest.required
    ]


def reset_registry() -> None:
    """Test-only helper to clear connector registrations."""
    _connectors.clear()


def check_required_connectors_installed() -> list[str]:
    missing: list[str] = []
    for connector in required_connectors():
        if not connector.is_installed():
            missing.append(connector.manifest.id)
            log.warning("required connector missing: %s", connector.manifest.id)
    return missing


def import_mode_invariant_violations() -> list[str]:
    source_importing = [
        connector.manifest.id
        for connector in _connectors.values()
        if connector.manifest.kind == ConnectorKind.SOURCE
        and connector.is_installed()
        and configured_mode(connector.manifest) == ConnectorMode.IMPORT.value
    ]
    if not source_importing:
        return []

    violations = [
        f"source connectors in import mode require installed verifier: {connector.manifest.id}"
        for connector in required_connectors()
        if not connector.is_installed()
        or configured_mode(connector.manifest) != ConnectorMode.IMPORT.value
    ]
    output_installed = any(
        connector.manifest.kind == ConnectorKind.OUTPUT
        and connector.is_installed()
        and configured_mode(connector.manifest) == ConnectorMode.IMPORT.value
        for connector in _connectors.values()
    )
    if not output_installed:
        violations.append(
            "source connectors in import mode require at least one installed output connector"
        )
    return violations


def connector_to_dict(connector: "Connector") -> dict:
    health = connector.health()
    config = config_for_manifest(connector.manifest)
    return {
        "id": connector.manifest.id,
        "kind": connector.manifest.kind.value,
        "display_name": connector.manifest.display_name,
        "manifest": manifest_to_dict(connector.manifest),
        "runtime": {
            "installed": connector.is_installed(),
            "enabled": config["enabled"],
            "mode": config["mode"],
            "config": config,
            "health": health.status,
            "version": connector.detected_version(),
            "last_error": health.last_error,
            "last_checked_at": unix_ts_to_iso(health.last_checked_at),
        },
    }


def registry_payload() -> dict:
    return {
        "api_version": MANIFEST_API_VERSION,
        "connectors": [connector_to_dict(connector) for connector in all_connectors()],
    }
