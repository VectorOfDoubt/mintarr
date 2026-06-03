# Connector Manifest — v1

> **Type:** Spec / contract
> **Version:** 1.0.0 — 2026-05-31
> **Status:** Runtime-backed v1 contract. Implemented by F4.1 static connector registry and exposed through `GET /dashboard/v1/connectors`.
> **Audience:** Connector authors. Mintarr maintainers when authoring connector manifests for built-in connectors. Dashboard / Integrations-tab authors consuming the manifest JSON.
> **Related:** [ADAPTER_PROTOCOL_v1.md](ADAPTER_PROTOCOL_v1.md), [ADR-0003](../architecture/adr/0003-connector-vs-adapter.md), [F4.1 design](../design/F4.1_STATIC_CONNECTOR_REGISTRY.md)

---

## 1. What this specifies

This document specifies the **`ConnectorManifest` dataclass** and the **runtime status JSON** Mintarr exposes via `GET /dashboard/v1/connectors`.

Adapters are versioned separately ([`ADAPTER_PROTOCOL_v1.md`](ADAPTER_PROTOCOL_v1.md)). A connector that wraps an adapter references the adapter; this spec does not duplicate adapter content.

## 2. Connector kinds

```python
class ConnectorKind(str, Enum):
    SOURCE = "source"       # Provides candidate files (e.g., TIDAL, LocalFolder)
    VERIFIER = "verifier"   # Produces evidence about files (e.g., ffprobe, FLAC Detective)
    OUTPUT = "output"       # Delivers files to a target (e.g., Lidarr ManualImport)
```

Mintarr maintainers may add new kinds in successor versions; v1 has three.

## 3. Connector modes

```python
class ConnectorMode(str, Enum):
    DISABLED = "disabled"   # Connector is registered but not used
    DRY_RUN = "dry_run"     # Verifier emits evidence without affecting policy decisions
    IMPORT = "import"       # Connector participates in production pipeline
```

`dry_run` is only meaningful for verifiers. Source and output connectors are either `disabled` or `import`.

## 4. `ConnectorManifest` dataclass

```python
@dataclass(frozen=True)
class ConnectorManifest:
    """Static metadata describing a connector. Loaded at boot, immutable
    for the life of the container. The fields here are exactly the fields
    serialised under "manifest" in GET /dashboard/v1/connectors."""

    # Identity
    id: str
    """Unique connector identifier. ^[a-z0-9_]+$.
    For source connectors, this is the operator-facing connector id.
    The adapter linkage is expressed separately through adapter_class."""

    display_name: str
    """Human-readable name for dashboard surface. Title-cased.
    Examples: 'TIDAL', 'FLAC Detective', 'Lidarr Manual Import'."""

    kind: ConnectorKind

    api_version: str
    """SemVer string identifying which CONNECTOR_MANIFEST spec this
    manifest conforms to. v1 manifests set this to '1.0.0'."""

    # Linkage to adapter (optional — verifiers and some outputs have none)
    adapter_class: str | None
    """Fully qualified module:class name of the underlying adapter, or
    None if there is no adapter. Example: 'adapters.tidal:TidalAdapter'."""

    # Operational defaults
    default_enabled: bool
    """Whether new installations have this connector enabled by default.
    Hard-gate verifiers (ffprobe, flac_t, flac_detective) set this True.
    Optional connectors (Soulseek, beets/Picard) set this False."""

    required: bool
    """True if disabling this connector while ANY source connector is in
    import mode is forbidden. Hard-gate verifiers are required=True."""

    # Install / deployment hints
    install_profile: str | None
    """docker-compose profile name that includes this connector's
    underlying service, or None if no compose profile is involved.
    Used in install-guidance display. Example: 'soulseek', 'beets'."""

    docker_service: str | None
    """Expected docker service name for the underlying tool, or None.
    Used in install-guidance display and connection-failure messages.
    Example: 'slskd', 'flac-detective'."""

    # Required and optional environment variables
    required_env: tuple[str, ...]
    """Environment variables the operator must set for this connector
    to be 'installed'. Used in install-guidance display.
    Example: ('SLSKD_API_URL', 'SLSKD_API_KEY')."""

    optional_env: tuple[str, ...]
    """Environment variables that fine-tune connector behaviour. Used
    in CONFIGURATION.md reference and connector-details display.
    Example: ('SOULSEEK_MAX_FILES', 'SOULSEEK_SETTLE_SECONDS')."""

    # Self-description
    capabilities: tuple[str, ...]
    """Free-form capability tags. Used in connector-details display
    and (eventually) for capability-based connector selection.
    Examples: ('spectral_analysis', 'fake_lossless_verdict'),
              ('hires_audio', 'oauth_session', 'subprocess_download')."""

    docs_url: str
    """Relative path or absolute URL to connector documentation.
    Surfaced in dashboard 'Learn more' link.
    Convention: relative path to MkDocs site, e.g.,
    'connectors/tidal/' or 'operations/CONFIGURATION.md#tidal'."""

    # Version compatibility
    min_supported_version: str | None
    """Minimum SemVer of the underlying external tool/service this
    connector supports. Mintarr probes the runtime version and
    compares; below this, the connector is marked incompatible and
    cannot enter import mode. None for connectors that do not have
    a versionable external tool."""
```

### 4.1 Field constraints

| Field | Constraint |
|---|---|
| `id` | Matches `^[a-z0-9_]+$`. Unique across the registry. |
| `display_name` | Non-empty UTF-8. Avoid emoji (dashboards may not render uniformly). |
| `kind` | One of the `ConnectorKind` values. |
| `api_version` | SemVer-parseable. v1 manifests use `'1.0.0'`. |
| `adapter_class` | None or `<module>:<ClassName>`. Module must be importable. |
| `default_enabled`, `required` | Bool. `required=True` implies `default_enabled=True`. |
| `install_profile`, `docker_service` | None or non-empty string. |
| `required_env`, `optional_env`, `capabilities` | Tuples of strings. Order not significant. |
| `docs_url` | Non-empty. Path relative to docs site or absolute URL. |
| `min_supported_version` | None or SemVer-parseable. |

## 5. `Connector` runtime contract

The runtime `Connector` object pairs a manifest with health and version detection.

```python
class Connector(Protocol):
    manifest: ConnectorManifest

    def is_installed(self) -> bool:
        """Returns True if the underlying tool/service exists and is
        reachable. For adapter-backed connectors, typically defers to
        adapter.is_enabled(). For verifier connectors (ffprobe), checks
        for the binary via PATH or HTTP probe.

        Cheap, synchronous. Cached for `installed_cache_seconds`
        (default 60s) by the runtime."""
        ...

    def is_enabled(self) -> bool:
        """Returns True if the operator has not disabled this connector.
        In v1, this returns `manifest.default_enabled` for connectors
        without a connector_config row, and the stored value otherwise."""
        ...

    def health(self) -> ConnectorHealth:
        """Synchronous health probe. Returns a status enum + optional
        error message. Cached for `health_cache_seconds` (default 60s).
        Probes that take more than 5 seconds time out as 'degraded'."""
        ...

    def detected_version(self) -> str | None:
        """Returns the detected version of the underlying tool, or None
        if version detection is not implemented or fails. Cached for
        container lifetime (no re-detection without explicit
        healthcheck POST)."""
        ...
```

### 5.1 `ConnectorHealth`

```python
@dataclass(frozen=True)
class ConnectorHealth:
    status: str               # See §5.2 for enum values
    last_error: str | None    # Short description if status != 'ok'
    last_checked_at: float    # Unix timestamp
```

### 5.2 Health status enum

| Value | Meaning |
|---|---|
| `ok` | Connector is reachable, version-compatible, and ready to do its job. |
| `degraded` | Connector responds but with reduced functionality (high latency, intermittent errors, near-limit rate-limit budget). |
| `blocked` | Connector cannot do its job right now but the operator has not disabled it (auth expired, downstream service down, version incompatibility). |
| `missing` | The underlying tool/service is not installed or not reachable. |
| `disabled` | Operator has explicitly disabled this connector via the dashboard. |

The dashboard displays each status with a distinct visual treatment (Phase 2 work).

## 6. Runtime status JSON shape

`GET /dashboard/v1/connectors` returns:

```json
{
  "api_version": "1.0.0",
  "connectors": [
    {
      "id": "tidal",
      "kind": "source",
      "display_name": "TIDAL",
      "manifest": {
        "id": "tidal",
        "display_name": "TIDAL",
        "kind": "source",
        "api_version": "1.0.0",
        "adapter_class": "adapters.tidal:TidalAdapter",
        "default_enabled": true,
        "required": false,
        "install_profile": null,
        "docker_service": null,
        "required_env": ["TIDAL_DL_NG_CONFIG"],
        "optional_env": ["TIDAL_OAUTH_PKCE"],
        "capabilities": ["hires_audio", "oauth_session", "subprocess_download"],
        "docs_url": "connectors/tidal/",
        "min_supported_version": null
      },
      "runtime": {
        "installed": true,
        "enabled": true,
        "mode": "import",
        "config": {
          "connector_id": "tidal",
          "enabled": true,
          "mode": "import",
          "updated_at": null,
          "actor": null
        },
        "health": "ok",
        "version": null,
        "last_error": null,
        "last_checked_at": "2026-05-26T18:30:00Z"
      },
      "install_guidance": {
        "show": false,
        "reason": "Connector is ready.",
        "actions": [],
        "required_env": ["TIDAL_DL_NG_CONFIG"],
        "optional_env": ["TIDAL_OAUTH_PKCE"],
        "docker_service": null,
        "install_profile": null,
        "docs_url": "connectors/tidal/",
        "min_supported_version": null
      }
    },
    {
      "id": "flac_detective",
      "kind": "verifier",
      "display_name": "FLAC Detective",
      "manifest": {
        "id": "flac_detective",
        "display_name": "FLAC Detective",
        "kind": "verifier",
        "api_version": "1.0.0",
        "adapter_class": null,
        "default_enabled": true,
        "required": true,
        "install_profile": null,
        "docker_service": "flac-detective",
        "required_env": ["FLAC_API_URL"],
        "optional_env": [],
        "capabilities": ["spectral_analysis", "fake_lossless_verdict", "per_file_evidence"],
        "docs_url": "connectors/flac_detective/",
        "min_supported_version": "0.6.0"
      },
      "runtime": {
        "installed": true,
        "enabled": true,
        "mode": "import",
        "health": "ok",
        "version": "0.7.1",
        "last_error": null,
        "last_checked_at": "2026-05-26T18:30:00Z"
      },
      "install_guidance": {
        "show": false,
        "reason": "Connector is ready.",
        "actions": [],
        "required_env": ["FLAC_API_URL"],
        "optional_env": [],
        "docker_service": "flac-detective",
        "install_profile": null,
        "docs_url": "connectors/flac_detective/",
        "min_supported_version": "0.6.0"
      }
    }
  ]
}
```

The response is secret-safe: it may include environment variable names and
service/profile hints, but must not include environment values, tokens, API
keys, local private host paths, or Docker socket access.

`install_guidance.show=true` means the dashboard should render the guidance
block. Typical triggers are `runtime.health` being `missing`, `blocked`, or
`disabled`, `runtime.installed=false`, or `runtime.mode=disabled`.

Connector config mutation is handled by
`POST /dashboard/v1/connectors/<connector_id>/config`.

## 7. Connector authoring conventions

These guide connector implementations to produce consistent operator experience.

### 7.1 `display_name`

Match the upstream product's preferred name. Don't reinvent: TIDAL (not Tidal), Lidarr (not LIDARR), Soulseek (not soulseek).

### 7.2 `capabilities`

Use snake_case. Reuse existing tags where possible:

- Audio quality: `hires_audio`, `lossless_audio`, `lossy_audio`
- Authentication: `oauth_session`, `api_key`, `no_auth`
- Source semantics: `streaming_service`, `peer_to_peer`, `local_filesystem`, `download_client`
- Verifier semantics: `hard_gate`, `spectral_analysis`, `metadata_identity`, `cd_rip_proof`
- Operational: `subprocess_download`, `http_download`, `requires_external_service`

New capabilities are added by including them in connector manifests; the registry has no enum constraint.

### 7.3 `docs_url`

Relative path to MkDocs Material site root. The dashboard renders this as the docs-site URL plus the relative path. Common patterns:

- `connectors/<id>/` — dedicated per-connector page (preferred for non-trivial connectors)
- `operations/CONFIGURATION.md#<connector_id>` — section anchor within configuration reference (preferred for trivial connectors)

### 7.4 `min_supported_version`

Set this when the connector talks to an external tool that has documented breaking-change history. Examples:

- `flac_detective`: matches the FLAC Detective project's stable API
- `slskd` (Phase 4): matches the slskd HTTP API contract
- Lidarr Manual Import: tracks Lidarr's v1 API surface

Leave as None for connectors talking to opaque tools (TIDAL streaming API is not version-stable in a meaningful way; ffprobe's version range we care about spans years).

## 8. Connector boot order

The registry is populated in this order in `app/server.py`:

1. Adapters are registered (the adapter registry — separate from connectors)
2. Connectors that wrap adapters are registered next, referencing the adapter classes
3. Standalone connectors (verifiers, outputs without adapters) are registered last

Boot fails loudly if a connector references a missing adapter. Boot logs a warning (does not fail) if a `required=True` connector reports `installed=False` — operators may run Mintarr in partial-install states during setup. Optional verifiers such as `picard_beets_acoustid` are registered in the same boot path, but default to disabled and must not affect import policy until a later policy change explicitly promotes their evidence.

## 9. Required-connector enforcement

The runtime enforces the following at the connector-config level (F4.3):

1. A source connector cannot enter `import` mode if any `required=True` verifier connector is `disabled`.
2. At least one output connector must be `enabled` and `installed` for any source connector to be in `import` mode.
3. A `required=True` connector cannot be moved to `disabled` while any source connector is in `import` mode.

Violations result in HTTP 409 from the connector-config endpoint and a dashboard warning. F4.1 surfaces the warnings as read-only; F4.3 enforces them on mutation.

## 10. Manifest registry

```python
# app/connectors/registry.py

def register(connector: Connector) -> None: ...
def get_connector(connector_id: str) -> Connector | None: ...
def all_connectors() -> list[Connector]: ...
def required_connectors() -> list[Connector]: ...
def enabled_connectors() -> list[Connector]: ...
def reset_registry() -> None: ...  # test-only
```

`enabled_connectors()` returns connectors where `is_installed()` and `is_enabled()` both return True.

## 11. Invariants

The Mintarr core enforces these about manifests:

1. **Connector IDs are unique.** Second registration of the same ID raises.
2. **Required source connectors do not exist.** Required is meaningful only for verifier and output connectors.
3. **Adapter-class references must resolve.** Boot fails if `adapter_class` references a missing module or class.
4. **`api_version` matches the spec file version.** v1 manifests must declare `'1.0.0'`.
5. **`required=True` implies `default_enabled=True`.** Asserted at registration.

Tests cover these.

## 12. Worked example — connector for an existing adapter

```python
# app/connectors/tidal.py

from connectors.base import ConnectorManifest, ConnectorKind
from adapters.tidal import TidalAdapter


class TidalConnector:
    manifest = ConnectorManifest(
        id="tidal",
        display_name="TIDAL",
        kind=ConnectorKind.SOURCE,
        api_version="1.0.0",
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
    )

    def __init__(self, adapter: TidalAdapter):
        self._adapter = adapter

    def is_installed(self) -> bool:
        return self._adapter.is_enabled()  # adapter checks token presence

    def is_enabled(self) -> bool:
        return True  # F4.1 — no per-connector config yet

    def health(self) -> ConnectorHealth:
        return ConnectorHealth(
            status="ok" if self.is_installed() else "missing",
            last_error=None if self.is_installed() else "no token.json — run tidal-dl-ng login",
            last_checked_at=time.time(),
        )

    def detected_version(self) -> str | None:
        return None  # TIDAL has no meaningful client version
```

## 13. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-05-26 | Initial locked spec. |

---

> Last updated: 2026-05-26
