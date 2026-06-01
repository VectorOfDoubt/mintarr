"""Built-in connector implementations."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .base import ConnectorHealth, ConnectorKind, ConnectorManifest, MANIFEST_API_VERSION, health_checked_now
from .config import configured_enabled
from .registry import check_required_connectors_installed, get_connector, register


@dataclass
class AdapterBackedConnector:
    manifest: ConnectorManifest
    adapter_name: str

    def _adapter(self):
        import adapters
        return adapters.get_adapter(self.adapter_name)

    def is_installed(self) -> bool:
        adapter = self._adapter()
        return bool(adapter and adapter.is_enabled())

    def is_enabled(self) -> bool:
        return configured_enabled(self.manifest)

    def health(self) -> ConnectorHealth:
        if not self.is_enabled():
            return health_checked_now("disabled")
        if not self.is_installed():
            return health_checked_now("missing", "adapter is not configured")
        return health_checked_now("ok")

    def detected_version(self) -> str | None:
        return None


@dataclass
class BinaryConnector:
    manifest: ConnectorManifest
    binary: str
    version_args: tuple[str, ...]
    version_parser: Callable[[str], str | None] | None = None
    _version_probed: bool = False
    _version_cache: str | None = None

    def is_installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def is_enabled(self) -> bool:
        return configured_enabled(self.manifest)

    def health(self) -> ConnectorHealth:
        if not self.is_enabled():
            return health_checked_now("disabled")
        if not self.is_installed():
            return health_checked_now("missing", f"{self.binary} not found in PATH")
        return health_checked_now("ok")

    def detected_version(self) -> str | None:
        if self._version_probed:
            return self._version_cache
        self._version_probed = True
        if not self.is_installed():
            return None
        try:
            result = subprocess.run(
                [self.binary, *self.version_args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return None
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if self.version_parser is not None:
            self._version_cache = self.version_parser(output)
            return self._version_cache
        first_line = output.strip().splitlines()[0] if output.strip() else ""
        self._version_cache = first_line or None
        return self._version_cache


class FlacDetectiveConnector:
    manifest = ConnectorManifest(
        id="flac_detective",
        display_name="FLAC Detective",
        kind=ConnectorKind.VERIFIER,
        api_version=MANIFEST_API_VERSION,
        adapter_class=None,
        default_enabled=True,
        required=True,
        install_profile=None,
        docker_service="flac-detective",
        required_env=("FLAC_API_URL",),
        optional_env=(),
        capabilities=("spectral_analysis", "fake_lossless_verdict", "per_file_evidence"),
        docs_url="connectors/flac_detective/",
        min_supported_version="0.6.0",
    )
    health_cache_seconds = 60.0

    def __init__(self) -> None:
        self._health_cache: ConnectorHealth | None = None

    def _health_url(self) -> str:
        analyze_url = os.environ.get("FLAC_API_URL", "http://host.docker.internal:8889/analyze")
        return analyze_url.rsplit("/", 1)[0].rstrip("/") + "/health"

    def is_installed(self) -> bool:
        return self.health().status in {"ok", "degraded"}

    def is_enabled(self) -> bool:
        return configured_enabled(self.manifest)

    def health(self) -> ConnectorHealth:
        if (
            self._health_cache is not None
            and time.time() - self._health_cache.last_checked_at < self.health_cache_seconds
        ):
            return self._health_cache
        if not self.is_enabled():
            self._health_cache = health_checked_now("disabled")
            return self._health_cache
        try:
            import requests
            response = requests.get(self._health_url(), timeout=2)
        except Exception as exc:
            self._health_cache = health_checked_now("missing", str(exc)[:200])
            return self._health_cache
        if response.ok:
            self._health_cache = health_checked_now("ok")
            return self._health_cache
        self._health_cache = health_checked_now("degraded", f"HTTP {response.status_code}")
        return self._health_cache

    def detected_version(self) -> str | None:
        return None


def _lidarr_api_url() -> str:
    return os.environ.get("LIDARR_API_URL", "http://host.docker.internal:8686/api/v1").rstrip("/")


def _lidarr_api_key() -> str:
    config_path = os.environ.get("LIDARR_CONFIG_XML", "/lidarr-config/config.xml")
    try:
        content = Path(config_path).read_text()
        match = re.search(r"<ApiKey>([A-Za-z0-9]+)</ApiKey>", content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return os.environ.get("LIDARR_API_KEY", "")


@dataclass
class LidarrConnector:
    manifest: ConnectorManifest
    health_cache_seconds: float = 60.0
    _health_cache: ConnectorHealth | None = None
    _version_probed: bool = False
    _version_cache: str | None = None

    def is_installed(self) -> bool:
        return self.health().status in {"ok", "degraded", "blocked"}

    def is_enabled(self) -> bool:
        try:
            import state_db
            stored = state_db.get_connector_config(self.manifest.id)
        except Exception:
            stored = None
        if stored is not None:
            return bool(stored.get("enabled"))
        if self.manifest.id == "lidarr_rescue_rescan":
            value = os.environ.get("MINTARR_RESCUE_RESCAN_ENABLED")
            if value is None:
                value = os.environ.get("TIDALHIRES_RESCUE_RESCAN_ENABLED")
            if value is not None:
                return value.lower() in {"1", "true", "yes", "on"}
        return configured_enabled(self.manifest)

    def health(self) -> ConnectorHealth:
        if (
            self._health_cache is not None
            and time.time() - self._health_cache.last_checked_at < self.health_cache_seconds
        ):
            return self._health_cache
        if not self.is_enabled():
            self._health_cache = health_checked_now("disabled")
            return self._health_cache
        key = _lidarr_api_key()
        if not key:
            self._health_cache = health_checked_now("missing", "Lidarr API key not configured")
            return self._health_cache
        try:
            import requests
            response = requests.get(
                f"{_lidarr_api_url()}/queue",
                headers={"X-Api-Key": key},
                params={"pageSize": 1},
                timeout=3,
            )
        except Exception as exc:
            self._health_cache = health_checked_now("missing", str(exc)[:200])
            return self._health_cache
        if response.ok:
            self._health_cache = health_checked_now("ok")
            return self._health_cache
        self._health_cache = health_checked_now("blocked", f"HTTP {response.status_code}")
        return self._health_cache

    def detected_version(self) -> str | None:
        if self._version_probed:
            return self._version_cache
        self._version_probed = True
        key = _lidarr_api_key()
        if not key:
            return None
        try:
            import requests
            response = requests.get(
                f"{_lidarr_api_url()}/system/status",
                headers={"X-Api-Key": key},
                timeout=3,
            )
        except Exception:
            return None
        if not response.ok:
            return None
        payload = response.json() or {}
        self._version_cache = payload.get("version")
        return self._version_cache


def _first_semver(output: str) -> str | None:
    match = re.search(r"\d+\.\d+(?:\.\d+)?", output)
    return match.group(0) if match else None


def built_in_connectors() -> list:
    return [
        AdapterBackedConnector(
            manifest=ConnectorManifest(
                id="tidal",
                display_name="TIDAL",
                kind=ConnectorKind.SOURCE,
                api_version=MANIFEST_API_VERSION,
                adapter_class="adapters.tidal:TidalAdapter",
                default_enabled=True,
                required=False,
                install_profile=None,
                docker_service=None,
                required_env=("TIDAL_DL_NG_CONFIG",),
                optional_env=("TIDAL_OAUTH_PKCE",),
                capabilities=("hires_audio", "oauth_session", "subprocess_download"),
                docs_url="connectors/tidal/",
                min_supported_version=None,
            ),
            adapter_name="tidal",
        ),
        AdapterBackedConnector(
            manifest=ConnectorManifest(
                id="local_folder",
                display_name="Local Folder",
                kind=ConnectorKind.SOURCE,
                api_version=MANIFEST_API_VERSION,
                adapter_class="adapters.local_folder:LocalFolderAdapter",
                default_enabled=True,
                required=False,
                install_profile=None,
                docker_service=None,
                required_env=("LOCAL_INGEST_PATH",),
                optional_env=(),
                capabilities=("local_ingest", "copy_source_files", "manual_enqueue"),
                docs_url="connectors/local_folder/",
                min_supported_version=None,
            ),
            adapter_name="local",
        ),
        AdapterBackedConnector(
            manifest=ConnectorManifest(
                id="soulseek",
                display_name="Soulseek",
                kind=ConnectorKind.SOURCE,
                api_version=MANIFEST_API_VERSION,
                adapter_class="adapters.soulseek:SoulseekCompletedAdapter",
                default_enabled=False,
                required=False,
                install_profile="soulseek",
                docker_service="slskd",
                required_env=("SOULSEEK_ENABLED", "SOULSEEK_DOWNLOAD_ROOT"),
                optional_env=(
                    "SOULSEEK_MAX_FILES",
                    "SOULSEEK_MAX_BYTES",
                    "SOULSEEK_SETTLE_SECONDS",
                    "SOULSEEK_SEARCH_ENABLED",
                    "SLSKD_API_URL",
                    "SLSKD_API_KEY",
                    "SOULSEEK_SEARCH_TIMEOUT",
                    "SOULSEEK_SEARCH_RESPONSE_LIMIT",
                    "SOULSEEK_SEARCH_FILE_LIMIT",
                    "SOULSEEK_SEARCH_SUFFIX",
                    "SOULSEEK_MIN_TRACKS",
                    "SOULSEEK_DOWNLOAD_TIMEOUT",
                    "SOULSEEK_POLL_SECONDS",
                ),
                capabilities=(
                    "completed_folder_ingest",
                    "slskd_search",
                    "slskd_download",
                    "copy_source_files",
                    "manual_enqueue",
                ),
                docs_url="connectors/soulseek/",
                min_supported_version=None,
            ),
            adapter_name="soulseek",
        ),
        BinaryConnector(
            manifest=ConnectorManifest(
                id="ffprobe",
                display_name="ffprobe",
                kind=ConnectorKind.VERIFIER,
                api_version=MANIFEST_API_VERSION,
                adapter_class=None,
                default_enabled=True,
                required=True,
                install_profile=None,
                docker_service=None,
                required_env=(),
                optional_env=(),
                capabilities=("codec_probe", "audio_stream_metadata"),
                docs_url="connectors/ffprobe/",
                min_supported_version=None,
            ),
            binary="ffprobe",
            version_args=("-version",),
            version_parser=_first_semver,
        ),
        BinaryConnector(
            manifest=ConnectorManifest(
                id="flac_t",
                display_name="flac -t",
                kind=ConnectorKind.VERIFIER,
                api_version=MANIFEST_API_VERSION,
                adapter_class=None,
                default_enabled=True,
                required=True,
                install_profile=None,
                docker_service=None,
                required_env=(),
                optional_env=(),
                capabilities=("flac_integrity", "decode_md5_check"),
                docs_url="connectors/flac_t/",
                min_supported_version=None,
            ),
            binary="flac",
            version_args=("--version",),
            version_parser=_first_semver,
        ),
        FlacDetectiveConnector(),
        LidarrConnector(
            manifest=ConnectorManifest(
                id="lidarr_manual_import",
                display_name="Lidarr Manual Import",
                kind=ConnectorKind.OUTPUT,
                api_version=MANIFEST_API_VERSION,
                adapter_class=None,
                default_enabled=True,
                required=False,
                install_profile=None,
                docker_service="lidarr",
                required_env=("LIDARR_API_URL",),
                optional_env=("LIDARR_API_KEY", "LIDARR_CONFIG_XML"),
                capabilities=("manual_import", "quality_profile_import"),
                docs_url="connectors/lidarr_manual_import/",
                min_supported_version=None,
            )
        ),
        LidarrConnector(
            manifest=ConnectorManifest(
                id="lidarr_rescue_rescan",
                display_name="Lidarr Rescue Rescan",
                kind=ConnectorKind.OUTPUT,
                api_version=MANIFEST_API_VERSION,
                adapter_class=None,
                default_enabled=False,
                required=False,
                install_profile=None,
                docker_service="lidarr",
                required_env=("LIDARR_API_URL",),
                optional_env=("LIDARR_API_KEY", "LIDARR_CONFIG_XML", "MINTARR_RESCUE_RESCAN_ENABLED"),
                capabilities=("rescue_rescan", "library_rescan"),
                docs_url="connectors/lidarr_rescue_rescan/",
                min_supported_version=None,
            )
        ),
    ]


def register_builtin_connectors(*, warn_missing_required: bool = True) -> None:
    for connector in built_in_connectors():
        if get_connector(connector.manifest.id) is None:
            register(connector)
    if warn_missing_required:
        check_required_connectors_installed()
