"""Connector runtime config helpers.

This module is intentionally small and SQLite-backed. It keeps connector
config out of manifests while letting the registry and dashboard share the
same validation rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import ConnectorKind, ConnectorManifest, ConnectorMode

if TYPE_CHECKING:
    from .base import Connector


def default_config(manifest: ConnectorManifest) -> dict:
    mode = ConnectorMode.IMPORT.value if manifest.default_enabled else ConnectorMode.DISABLED.value
    return {
        "connector_id": manifest.id,
        "enabled": manifest.default_enabled,
        "mode": mode,
        "updated_at": None,
        "actor": None,
    }


def _stored_config(connector_id: str) -> dict | None:
    try:
        import state_db
        return state_db.get_connector_config(connector_id)
    except Exception:
        return None


def config_for_manifest(manifest: ConnectorManifest, overrides: dict[str, dict] | None = None) -> dict:
    row = (overrides or {}).get(manifest.id)
    if row is None:
        row = _stored_config(manifest.id)
    if not row:
        return default_config(manifest)
    mode = row.get("mode") or ConnectorMode.DISABLED.value
    enabled = bool(row.get("enabled"))
    if mode == ConnectorMode.DISABLED.value:
        enabled = False
    elif mode in {ConnectorMode.DRY_RUN.value, ConnectorMode.IMPORT.value}:
        enabled = True
    return {
        "connector_id": manifest.id,
        "enabled": enabled,
        "mode": mode,
        "updated_at": row.get("updated_at"),
        "actor": row.get("actor"),
    }


def configured_enabled(manifest: ConnectorManifest) -> bool:
    return bool(config_for_manifest(manifest)["enabled"])


def configured_mode(manifest: ConnectorManifest) -> str:
    return str(config_for_manifest(manifest)["mode"])


def normalize_update(*, enabled: bool | None, mode: str | None, manifest: ConnectorManifest) -> dict:
    current = config_for_manifest(manifest)
    next_mode = mode or current["mode"]
    try:
        next_mode = ConnectorMode(next_mode).value
    except ValueError as exc:
        raise ValueError(f"invalid connector mode: {next_mode}") from exc

    if enabled is None:
        next_enabled = next_mode != ConnectorMode.DISABLED.value
    else:
        next_enabled = bool(enabled)
    if not next_enabled:
        next_mode = ConnectorMode.DISABLED.value
    elif next_mode == ConnectorMode.DISABLED.value:
        next_enabled = False
    return {
        "connector_id": manifest.id,
        "enabled": next_enabled,
        "mode": next_mode,
        "updated_at": None,
        "actor": "user_dashboard",
    }


def _snapshot() -> dict[str, dict]:
    try:
        import state_db
        return state_db.list_connector_config()
    except Exception:
        return {}


def validate_connector_update(
    connector_id: str,
    *,
    enabled: bool | None,
    mode: str | None,
    connectors: list["Connector"],
) -> tuple[dict | None, list[str]]:
    connector = next((item for item in connectors if item.manifest.id == connector_id), None)
    if connector is None:
        return None, ["unknown connector"]

    try:
        proposed = normalize_update(enabled=enabled, mode=mode, manifest=connector.manifest)
    except ValueError as exc:
        return None, [str(exc)]

    errors: list[str] = []
    if connector.manifest.required and proposed["mode"] != ConnectorMode.IMPORT.value:
        errors.append("required connectors must stay in import mode")

    snapshot = _snapshot()
    snapshot[connector_id] = proposed

    source_importing = [
        item
        for item in connectors
        if item.manifest.kind == ConnectorKind.SOURCE
        and item.is_installed()
        and config_for_manifest(item.manifest, snapshot)["mode"] == ConnectorMode.IMPORT.value
    ]
    if source_importing:
        for item in connectors:
            if item.manifest.required and (
                not item.is_installed()
                or config_for_manifest(item.manifest, snapshot)["mode"] != ConnectorMode.IMPORT.value
            ):
                errors.append(
                    f"source connectors in import mode require installed verifier: {item.manifest.id}"
                )
        output_ready = any(
            item.manifest.kind == ConnectorKind.OUTPUT
            and item.is_installed()
            and config_for_manifest(item.manifest, snapshot)["mode"] == ConnectorMode.IMPORT.value
            for item in connectors
        )
        if not output_ready:
            errors.append("source connectors in import mode require at least one installed output connector")

    return proposed, errors


def persist_connector_config(config: dict) -> dict | None:
    try:
        import state_db
        return state_db.set_connector_config(
            config["connector_id"],
            enabled=bool(config["enabled"]),
            mode=str(config["mode"]),
            actor=str(config.get("actor") or "user_dashboard"),
        )
    except Exception:
        return None
