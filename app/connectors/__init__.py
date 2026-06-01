"""Connector registry package."""

from .base import (
    MANIFEST_API_VERSION,
    Connector,
    ConnectorHealth,
    ConnectorKind,
    ConnectorManifest,
    ConnectorMode,
    health_checked_now,
    manifest_from_dict,
    manifest_to_dict,
)
from .builtins import (
    AdapterBackedConnector,
    BinaryConnector,
    FlacDetectiveConnector,
    LidarrConnector,
    built_in_connectors,
    register_builtin_connectors,
)
from .registry import (
    all_connectors,
    check_required_connectors_installed,
    connector_to_dict,
    get_connector,
    import_mode_invariant_violations,
    registry_payload,
    register,
    required_connectors,
    reset_registry,
)

__all__ = [
    "MANIFEST_API_VERSION",
    "AdapterBackedConnector",
    "BinaryConnector",
    "Connector",
    "ConnectorHealth",
    "ConnectorKind",
    "ConnectorManifest",
    "ConnectorMode",
    "FlacDetectiveConnector",
    "LidarrConnector",
    "all_connectors",
    "built_in_connectors",
    "check_required_connectors_installed",
    "connector_to_dict",
    "get_connector",
    "health_checked_now",
    "import_mode_invariant_violations",
    "manifest_from_dict",
    "manifest_to_dict",
    "register",
    "register_builtin_connectors",
    "registry_payload",
    "required_connectors",
    "reset_registry",
]
