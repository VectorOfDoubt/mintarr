"""Connector manifest and runtime Protocol.

Connectors are the operator-facing integration surface. They wrap adapters
when an adapter exists, and stand alone for tools such as ffprobe or Lidarr.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

MANIFEST_API_VERSION = "1.0.0"

_CONNECTOR_ID_RE = re.compile(r"^[a-z0-9_]+$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class ConnectorKind(str, Enum):
    SOURCE = "source"
    VERIFIER = "verifier"
    OUTPUT = "output"


class ConnectorMode(str, Enum):
    DISABLED = "disabled"
    DRY_RUN = "dry_run"
    IMPORT = "import"


@dataclass(frozen=True)
class ConnectorManifest:
    id: str
    display_name: str
    kind: ConnectorKind
    api_version: str
    adapter_class: str | None
    default_enabled: bool
    required: bool
    install_profile: str | None
    docker_service: str | None
    required_env: tuple[str, ...]
    optional_env: tuple[str, ...]
    capabilities: tuple[str, ...]
    docs_url: str
    min_supported_version: str | None

    def __post_init__(self) -> None:
        if not _CONNECTOR_ID_RE.match(self.id):
            raise ValueError(f"invalid connector id: {self.id!r}")
        if not self.display_name:
            raise ValueError("display_name must be non-empty")
        if not isinstance(self.kind, ConnectorKind):
            object.__setattr__(self, "kind", ConnectorKind(self.kind))
        if not _SEMVER_RE.match(self.api_version):
            raise ValueError(f"invalid api_version: {self.api_version!r}")
        if self.required and not self.default_enabled:
            raise ValueError("required=True implies default_enabled=True")
        if self.adapter_class is not None and ":" not in self.adapter_class:
            raise ValueError("adapter_class must use module:ClassName")
        for name in ("install_profile", "docker_service", "docs_url"):
            value = getattr(self, name)
            if value is not None and not str(value):
                raise ValueError(f"{name} must be non-empty")
        if not self.docs_url:
            raise ValueError("docs_url must be non-empty")
        if self.min_supported_version is not None and not _SEMVER_RE.match(self.min_supported_version):
            raise ValueError(f"invalid min_supported_version: {self.min_supported_version!r}")
        for field_name in ("required_env", "optional_env", "capabilities"):
            values = tuple(getattr(self, field_name))
            if not all(isinstance(value, str) and value for value in values):
                raise ValueError(f"{field_name} must contain non-empty strings")
            object.__setattr__(self, field_name, values)


@dataclass(frozen=True)
class ConnectorHealth:
    status: str
    last_error: str | None
    last_checked_at: float


@runtime_checkable
class Connector(Protocol):
    manifest: ConnectorManifest

    def is_installed(self) -> bool:
        ...

    def is_enabled(self) -> bool:
        ...

    def health(self) -> ConnectorHealth:
        ...

    def detected_version(self) -> str | None:
        ...


def manifest_to_dict(manifest: ConnectorManifest) -> dict[str, Any]:
    return {
        "id": manifest.id,
        "display_name": manifest.display_name,
        "kind": manifest.kind.value,
        "api_version": manifest.api_version,
        "adapter_class": manifest.adapter_class,
        "default_enabled": manifest.default_enabled,
        "required": manifest.required,
        "install_profile": manifest.install_profile,
        "docker_service": manifest.docker_service,
        "required_env": list(manifest.required_env),
        "optional_env": list(manifest.optional_env),
        "capabilities": list(manifest.capabilities),
        "docs_url": manifest.docs_url,
        "min_supported_version": manifest.min_supported_version,
    }


def manifest_from_dict(data: dict[str, Any]) -> ConnectorManifest:
    return ConnectorManifest(
        id=data["id"],
        display_name=data["display_name"],
        kind=ConnectorKind(data["kind"]),
        api_version=data["api_version"],
        adapter_class=data.get("adapter_class"),
        default_enabled=bool(data["default_enabled"]),
        required=bool(data["required"]),
        install_profile=data.get("install_profile"),
        docker_service=data.get("docker_service"),
        required_env=tuple(data.get("required_env") or ()),
        optional_env=tuple(data.get("optional_env") or ()),
        capabilities=tuple(data.get("capabilities") or ()),
        docs_url=data["docs_url"],
        min_supported_version=data.get("min_supported_version"),
    )


def health_checked_now(status: str, last_error: str | None = None) -> ConnectorHealth:
    return ConnectorHealth(
        status=status,
        last_error=last_error,
        last_checked_at=datetime.now(timezone.utc).timestamp(),
    )


def unix_ts_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
