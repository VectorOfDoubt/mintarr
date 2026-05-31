# Mintarr Connector / Plugin Architecture

> **Type:** Architecture plan for dynamic stack visibility and connector management
> **Version:** 0.1 — 2026-05-26
> **Status:** Draft. Written for Claude/Codex review before implementation.
> **Related documents:** F3 source-adapter design, quality-stack roadmap, dashboard strategy and pipeline V2 plan (held in private monorepo pending v0.2.0 migration).

---

## 1. Problem

Mintarr is evolving from a TIDAL-only helper into a shared pre-import QC
control filter for the whole Lidarr music import surface. That creates a new
operator problem:

- Which source programs are installed?
- Which ones are running?
- Which ones are enabled for import?
- Which verifier tools are available?
- Which outputs can import or rescue files?
- What broke after a container or upstream tool update?
- Can the operator enable/disable one lane without editing code?

The current `SourceAdapter` abstraction answers only one slice: "how does a
source fetch raw files?" It does not cover install status, health, UI config,
version compatibility, verifier tools, or output targets.

## 2. Decision

Keep `SourceAdapter`, but place it under a broader **Connector** model.

Terminology:

```text
Connector
  SourceAdapter
  VerifierAdapter
  OutputAdapter
```

- **Adapter** = code contract used by the pipeline.
- **Connector** = operator-facing integration: manifest, config, enable/disable,
  health, version, dashboard panel and install guidance.

This is not a full third-party plugin system yet. F4 should implement a static
connector registry first.

## 3. Non-goal: dynamic third-party plugins now

Do **not** implement arbitrary Python plugin loading, pip-install from UI, or
user-uploaded connector code in the first version.

Reasons:

- security surface is large
- version compatibility needs a contract first
- Docker/Compose owns most actual installation concerns today
- the repo still changes quickly; dynamic plugins would freeze unstable APIs too
  early

The first version should feel plugin-like to the operator while remaining
static and testable in the repo.

## 4. Target shape

```text
Core service
  state_db
  worker queue
  verification_score
  policy
  Lidarr client
  audit / sidecars
  API / dashboard
  connector registry

Connectors
  Source connectors
    tidal
    local_folder
    soulseek_slskd
    private_tracker_cd_rip
    tubifarry_youtube_fallback
    future sab_usenet
    future qbittorrent_torrent

  Verifier connectors
    ffprobe
    flac_t
    flac_detective
    cuetools_ctdb
    picard_beets_acoustid
    optional audiocheckr

  Output connectors
    lidarr_manual_import
    lidarr_rescue_rescan
    future direct_library_import
```

Core owns policy and state. Connectors provide capabilities and evidence.

## 5. Connector manifest

Each connector should expose a small static manifest plus runtime health.

```python
@dataclass(frozen=True)
class ConnectorManifest:
    id: str                         # "tidal", "flac_detective"
    display_name: str               # "TIDAL", "FLAC Detective"
    kind: str                       # "source" | "verifier" | "output"
    adapter_class: str | None
    default_enabled: bool
    required: bool                  # true for ffprobe/flac_t in import mode
    install_profile: str | None     # docker compose profile/service hint
    docker_service: str | None
    required_env: list[str]
    optional_env: list[str]
    capabilities: list[str]
    docs: str
    min_supported_version: str | None
```

Runtime status:

```json
{
  "id": "flac_detective",
  "kind": "verifier",
  "installed": true,
  "enabled": true,
  "required": true,
  "health": "ok",
  "version": "0.7.1",
  "last_error": null,
  "last_checked_at": "2026-05-26T18:30:00Z",
  "capabilities": ["spectral_analysis", "fake_lossless_verdict"],
  "mode": "import",
  "dry_run": false
}
```

## 6. Connector types

### 6.1 Source connectors

Source connectors find or provide candidate audio files.

Examples:

| Connector | Status | Notes |
|---|---|---|
| `tidal` | implemented | reference lane |
| `local_folder` | implemented | manual/CD-rip style ingest |
| `soulseek_slskd` | F3.5 design | completed-folder first, HTTP search later |
| `private_tracker_cd_rip` | future | torrent/CD evidence lane |
| `sab_usenet` | future | completed-download/category ingest |
| `qbittorrent_torrent` | future | completed-category ingest |
| `tubifarry_youtube` | future/fallback | never treated as lossless upgrade |

Source connector invariants:

- no source bypasses shared QC
- `source_type` is persisted in jobs, records and sidecars
- weak provenance is context, not proof
- new connectors start disabled or dry-run
- each source has an explicit concurrency limit

### 6.2 Verifier connectors

Verifier connectors produce evidence.

Examples:

| Connector | Class | Import-mode rule |
|---|---|---|
| `ffprobe` | hard gate | required |
| `flac_t` | hard gate | required for FLAC |
| `flac_detective` | spectral heuristic | required in current V2 import mode |
| `cuetools_ctdb` | source-specific proof | optional, CD-rip lane only |
| `picard_beets_acoustid` | metadata identity | optional, read-only first |
| `audiocheckr` | optional heuristic | dry-run first |

Verifier connector invariants:

- hard gates cannot be disabled while a source connector is in import mode
- optional sensors can be enabled in dry-run to collect evidence without policy
  impact
- sensor evidence must include version, runtime, status, confidence domain and
  raw evidence summary
- metadata identity is not audio quality proof

### 6.3 Output connectors

Output connectors perform the final delivery/import.

Examples:

| Connector | Status | Notes |
|---|---|---|
| `lidarr_manual_import` | implemented | default output |
| `lidarr_rescue_rescan` | implemented | fallback/rescue |
| `direct_library_import` | future | only if Lidarr import is not enough |

Output connector invariants:

- write actions are explicit and auditable
- output connector errors must preserve verification decision
- rescue/rescan is import outcome, not verification outcome

## 7. Dashboard requirements

The dashboard should gain an **Integrations** view. It should be operational, not
marketing-style.

Top-level sections:

1. **Sources**
   - TIDAL
   - LocalFolder
   - Soulseek/slskd
   - future SAB/qBittorrent/private-tracker/Tubifarry

2. **Verifiers**
   - ffprobe
   - flac -t
   - FLAC Detective
   - CUETools/CTDB
   - beets/Picard/AcoustID

3. **Outputs**
   - Lidarr ManualImport
   - Lidarr rescue/rescan

Each connector card/row should show:

- installed / missing
- enabled / disabled
- mode: `dry_run` or `import`
- health: `ok`, `degraded`, `blocked`, `missing`, `disabled`
- detected version
- min supported version
- last check
- last error
- required env/config fields
- docker service / compose profile hint
- recent activity count

The main dashboard summary should stay focused on "do I need to act now?".
Connector details belong in the Integrations view.

## 8. Enable / disable model

First implementation should use state_db-backed config:

```text
connector_config
  id TEXT PRIMARY KEY
  enabled INTEGER
  mode TEXT                 -- dry_run | import
  config_json TEXT           -- non-secret settings only
  updated_at TEXT
```

Secrets remain environment variables or Docker secrets, not browser-visible DB
fields.

Rules:

- `enabled=false` means connector is not used by pipeline/search.
- `mode=dry_run` means evidence can be collected, but connector cannot cause
  Lidarr import.
- `mode=import` is allowed only if health is ok and required hard gates are
  enabled.
- required core connectors can be shown but not disabled from UI.

## 9. Installation and updates

Do not build app-managed install/uninstall yet.

Use Docker Compose profiles/services as the installation boundary:

```text
profile: tidal
profile: soulseek
profile: beets
profile: cuetools
profile: audiocheckr
```

The dashboard should report:

- service not found / not running
- API unreachable
- version missing
- version unsupported
- required env missing
- required mount missing

It may show operator guidance such as:

```text
slskd is not installed or not reachable.
Enable compose profile "soulseek" and set SLSKD_API_URL / SOULSEEK_DOWNLOAD_ROOT.
```

Update management should be informational first:

- detected version
- min supported version
- known-incompatible flag
- last health error after update

Actual container updates remain outside Mintarr until the connector API is
stable.

## 10. Beets / Picard / AcoustID placement

beets/Picard/AcoustID should be a **Verifier connector**, not a Source connector.

Role:

- identify likely MusicBrainz release/recording
- expose tracklist/release confidence
- help explain Lidarr match failures
- help future metadata lane / release matching

Limits:

- not audio quality proof
- not lossless authenticity proof
- read-only first
- no tag-writing until Lidarr tag ownership is explicitly designed
- no core score component until the prepass exists and has dogfood evidence

Dashboard:

- Beets/Picard deserves its own verifier row/card in Integrations.
- Record drawer may show "Identity evidence" when available.
- Missing beets/Picard should not block import unless a future source lane marks
  it required.

## 11. API surface

Future endpoints:

```text
GET  /dashboard/v1/connectors
GET  /dashboard/v1/connectors/<id>
POST /dashboard/v1/connectors/<id>/config
POST /dashboard/v1/connectors/<id>/enable
POST /dashboard/v1/connectors/<id>/disable
POST /dashboard/v1/connectors/<id>/healthcheck
```

Initial `GET /connectors` response shape:

```json
{
  "connectors": [
    {
      "id": "tidal",
      "kind": "source",
      "display_name": "TIDAL",
      "installed": true,
      "enabled": true,
      "mode": "import",
      "health": "ok",
      "version": null,
      "docker_service": "mintarr",
      "required_env": ["TIDAL_DL_NG_CONFIG"],
      "last_error": null
    }
  ]
}
```

Write endpoints need explicit confirmation in UI and audit actions in state_db.

## 12. Implementation phases

### F4.1 — Static connector registry

Scope:

- `connectors/base.py`
- `connectors/registry.py`
- manifests for existing integrations:
  - `tidal`
  - `local_folder`
  - `ffprobe`
  - `flac_t`
  - `flac_detective`
  - `lidarr_manual_import`
  - `lidarr_rescue_rescan`
- `GET /dashboard/v1/connectors`
- tests for manifest shape and required connector rules

No UI toggles yet.

### F4.2 — Health and version surface

Scope:

- per-connector health functions
- version detection where cheap/safe
- dashboard Integrations tab
- stack summary uses connector health instead of bespoke hardcoded checks where
  possible

### F4.3 — Enable/disable + dry-run config

Scope:

- `connector_config` table
- UI toggles for optional connectors
- hard gate disable protection
- audit trail for connector config changes
- `dry_run` vs `import` mode

### F4.4 — Compose profile/install guidance

Scope:

- show missing services/mounts/env
- document profiles
- no app-managed docker updates

### F4.5 — Optional verifier connectors

Scope:

- Beets/Picard/AcoustID read-only prepass design
- CUETools/CTDB connector design for CD-rip lane
- optional sensors start in dry-run

## 13. Relationship to F3.5

F3.5a Soulseek can be implemented before F4 if we want quick value.

But if the goal is a polished, dynamic dashboard and a cleaner long-term stack,
F4.1 should happen before F3.5b. F3.5b introduces slskd API health, source
enablement, search exposure and version concerns; those fit better with the
connector registry than with more hardcoded dashboard logic.

Recommended order:

```text
F3.5a Soulseek completed-folder ingest
F4.1 static connector registry
F4.2 Integrations dashboard
F4.3 connector enable/disable + dry-run
F3.5b slskd HTTP search/download
F4.5 beets/Picard and CUETools verifier connectors
```

## 14. Open questions for Claude/Codex

1. Should F4.1 happen before F3.5a, or is F3.5a small enough to land first?
2. Should connector config live in state_db only, or also support a checked-in
   YAML/TOML file for reproducibility?
3. Should connector health be polled on dashboard load, background scheduled, or
   both?
4. Should `flac_detective` remain a required verifier while unavailable causes
   BLOCK, or should it support dry-run only for some future source lanes?
5. Should Beets/Picard be one connector or two separate connectors?
6. What is the minimum useful version contract for external services?
7. Should compose profiles be documented only, or should Mintarr inspect
   Docker labels/services when mounted docker socket is available? Default
   recommendation: do not mount docker socket.

## 15. Locked invariants

These should survive the connector work:

1. No source connector bypasses shared QC before Lidarr import.
2. Hard gates cannot be disabled in import mode.
3. Optional verifiers start in dry-run until proven.
4. Secrets are not stored in browser-visible config.
5. Dashboard can enable/disable optional connectors, but not install arbitrary
   code.
6. Docker socket is not mounted into Mintarr by default.
7. Metadata identity connectors do not prove audio quality.
8. Output connectors record import outcome, not verification decision.
9. Connector health is operational context, not policy by itself.
10. Dynamic third-party plugin loading is deferred until the static registry is
    stable and tested.

## Changelog

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-05-26 | Initial connector/plugin architecture plan. Defines static connector registry, source/verifier/output connector types, Integrations dashboard, config model and F4 phases. |
