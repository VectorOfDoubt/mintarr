# Lidarr Integration

> **Type:** Spec / cross-cutting reference
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Updated as new Lidarr versions are tested and as the multi-version client lands.
> **Audience:** Contributors touching Mintarr's Lidarr-facing code. Operators upgrading Lidarr who want to know what to expect.

---

## 1. Why this exists

Mintarr depends on Lidarr's HTTP API. Lidarr's API surface evolves; Lidarr v4 (currently in development) changes things v3 callers rely on. Without an explicit Lidarr-integration contract, every Lidarr upgrade risks breaking Mintarr silently.

This document specifies:

- Which Lidarr versions Mintarr supports
- Which Lidarr API endpoints Mintarr calls
- How Mintarr detects the running Lidarr version
- How the multi-version client (planned, Phase 4 cross-cutting) handles version differences
- How operators should approach Lidarr upgrades

## 2. Supported Lidarr versions

| Lidarr version | Status | Notes |
|---|---|---|
| 3.0.x | Not tested | Pre-2022; missing some endpoints Mintarr uses |
| 3.1.x | **Supported** | Reference version. Current Mintarr code is tested against `3.1.0.4875`. |
| 4.x (preview) | Not supported yet | Rewrite in progress; capability-detection client lands when v4 stabilises |

A Lidarr version is "supported" if Mintarr's tests run against a mock of its API in CI.

## 3. Lidarr endpoints Mintarr calls

All endpoints are GET unless noted otherwise. `<base>` is `LIDARR_API_URL` (e.g., `http://lidarr:8686/api/v1`).

### 3.1 Health and identity

| Endpoint | Purpose | Required for |
|---|---|---|
| `GET <base>/system/status` | Get Lidarr version, branch, runtime info | Planned boot-time capability detection (Phase 4 client; not called by current runtime) |

### 3.2 Search and metadata

| Endpoint | Purpose | Required for |
|---|---|---|
| `GET <base>/album?artistId=<id>` | List albums by artist | Rescue path (build fake files when manualimport returns empty) |
| `GET <base>/album/<id>` | Get album with statistics + releases | Release-switch logic (multi-album bug mitigation) |
| `PUT <base>/album/<id>` | Update Lidarr album metadata/release selection during release-switch flow | Release-switch logic (multi-album bug mitigation) |
| `GET <base>/artist/<id>` | Get artist metadata/path for rescue placement | Rescue / place-files-and-rescan path |
| `GET <base>/track?albumReleaseId=<id>` | Get tracks in a release | Release-switch scoring |
| `GET <base>/trackfile?albumId=<id>` | Get existing track files (current library state) | Pre-import existing-quality lookup |
| `GET <base>/search?term=<query>` | Search Lidarr's index | Rescue path (find Lidarr album by title) |

### 3.3 Import paths

| Endpoint | Purpose | Required for |
|---|---|---|
| `GET <base>/manualimport?folder=<path>` | Lidarr's analysis of which files in `<path>` can be imported, with rejection reasons | Primary import path |
| `POST <base>/command` with `name=ManualImport` | Trigger Lidarr to import a list of files | Primary import path |
| `POST <base>/command` with `name=RescanFolder` | Trigger Lidarr to rescan a library folder | Rescue / place-files-and-rescan path |
| `GET <base>/command/<id>` | Poll a Lidarr command for completion | Both import paths |
| `GET <base>/command` | List active commands | Dashboard health / stuck-command detection |

### 3.4 History and blocklist

| Endpoint | Purpose | Required for |
|---|---|---|
| `GET <base>/history?pageSize=...` | Lidarr's grab/import history | Blocklist trigger lookup and dashboard context |
| `POST <base>/history/failed/<id>` | Mark a Lidarr history entry as failed (triggers blocklist) | BLOCK decision handling |

### 3.5 Queue

| Endpoint | Purpose | Required for |
|---|---|---|
| `GET <base>/queue?pageSize=...&includeUnknownArtistItems=true` | Lidarr's download queue | Queue cleanup after import and dashboard context |
| `GET <base>/queue?pageSize=1` | Lightweight queue count probe | Dashboard summary |
| `DELETE <base>/queue/<id>?removeFromClient=true&blocklist=true&skipRedownload=true` | Remove a queue entry | Queue cleanup |

## 4. Authentication

Mintarr authenticates to Lidarr via API key in the `X-Api-Key` header. The key is obtained from:

1. `LIDARR_API_KEY` environment variable (if set), OR
2. Read from `<ApiKey>` element in `/lidarr-config/config.xml` (if `LIDARR_CONFIG_XML` is set)

The XML-extraction path is preferred for compose setups where operators mount Lidarr's config directory read-only. It avoids putting the key in Mintarr's environment.

## 5. Capability detection (multi-version client — planned)

The current Mintarr code (`tidalhires/app/server.py`) calls Lidarr endpoints directly. The Phase 4 cross-cutting work introduces a `LidarrClient` interface with version-specific implementations:

```python
# Planned: app/lidarr/__init__.py

class LidarrClient(Protocol):
    """Version-agnostic Lidarr API client. Concrete implementations
    exist per major version (v3, v4). Mintarr selects the right one
    at boot via capability detection."""

    api_version: str  # '3', '4'

    def system_status(self) -> dict: ...
    def manual_import_lookup(self, folder: str) -> list[dict]: ...
    def manual_import_execute(self, files: list[dict]) -> int: ...  # returns command id
    def command_status(self, command_id: int) -> dict: ...
    def album_get(self, album_id: int) -> dict: ...
    def album_releases(self, album_id: int) -> list[dict]: ...
    def trackfile_list(self, album_id: int) -> list[dict]: ...
    def queue_list(self) -> list[dict]: ...
    def queue_delete(self, queue_id: int, *, blocklist: bool) -> None: ...
    def history_list(self, page_size: int) -> list[dict]: ...
    def history_mark_failed(self, history_id: int) -> None: ...
    def search(self, term: str) -> list[dict]: ...
    def rescan_folder(self, path: str) -> int: ...  # returns command id


class LidarrV1Client:
    """Implementation for Lidarr v3.x using /api/v1 endpoints."""
    api_version = "3"
    # ... concrete impls


class LidarrV2Client:  # planned for Lidarr v4 stabilisation
    """Implementation for Lidarr v4.x using /api/v2 endpoints
    (or whatever shape v4 settles on)."""
    api_version = "4"
    # ... concrete impls


def detect_lidarr_client(base_url: str, api_key: str) -> LidarrClient:
    """Probe /api/v1/system/status, then /api/v2/system/status if v1
    returns 404. Select the matching client. Cache the choice for the
    container lifetime."""
    ...
```

Until this lands, Mintarr's Lidarr coupling lives in `server.py` and assumes v3 endpoints. The migration plan is documented as part of the Phase 4 work in [ROADMAP.md](../strategy/ROADMAP.md).

## 6. Version-specific behaviour

### 6.1 Lidarr 3.1.x

Reference implementation. All endpoints in §3 work as documented in Lidarr's [API documentation](https://lidarr.audio/docs/api/).

Quirks:

- `GET /api/v1/manualimport?folder=...` may return rejections like `"Album match is not close enough: 70.1 % vs 80 %"` for valid imports of edition variants. Mintarr force-imports these via the release-family rejection allow-list.
- The 80%-match heuristic occasionally causes Lidarr to refuse imports of files whose tracklist disagrees with the matched album release. Mintarr's release-switch logic (in `_trigger_lidarr_import`) selects a better-matching release before retrying.
- `POST /api/v1/command` returns immediately with `status="queued"` or `status="started"`. Mintarr polls via `GET /api/v1/command/<id>` until terminal (or until `_lidarr_command_still_pending` returns False).

### 6.2 Lidarr 4.x (preview)

Mintarr does not support Lidarr 4 yet. The rewrite changes:

- API path versioning (`/api/v1/` → likely `/api/v2/`)
- Some endpoint shapes
- Authentication mechanisms
- Frontend separated from backend

When Lidarr 4 stabilises, `LidarrV2Client` lands with the version-specific differences captured. Mintarr's boot-time capability detection selects v3 or v4 client automatically.

## 7. Custom Format conventions

Mintarr does not configure Lidarr's Custom Formats — that's Lidarr's domain ([ADR-0008 boundary](../architecture/adr/0008-strategic-positioning.md)). But Mintarr produces release titles in formats Lidarr's CF system can score.

Source tags in Mintarr release titles:

| Source | Tag in title | Suggested CF |
|---|---|---|
| TIDAL | `[TIDAL]` | `Mintarr-tidal` regex `\[TIDAL\]` score +50 |
| LocalFolder | `[Local]` | `Mintarr-local` regex `\[Local\]` score +20 |
| Soulseek (planned) | `[Soulseek]` | `Mintarr-soulseek` regex `\[Soulseek\]` score -10 |
| Future SAB / qBit / CD-rip | `[<Source>]` | Per source, operator chooses |

Quality tags (`[FLAC 24bit]`, `[FLAC]`, `[MP3 320]`) follow standard Lidarr-parser conventions and are not Mintarr-specific.

Documenting recommended CF scores is operator-documentation work, not Mintarr-code work. The recommendations live in [`docs/operations/CONFIGURATION.md`](../operations/CONFIGURATION.md).

## 8. Upgrade and compatibility expectations

### 8.1 Operator upgrading Lidarr

When an operator upgrades Lidarr:

1. Lidarr's `system/status` endpoint exposes the new version
2. Mintarr's next boot detects the version (post Phase 4 work — until then operators must update Mintarr in lockstep with Lidarr v3→v4)
3. If Mintarr does not have a client for the new Lidarr major version, dashboard surfaces `incompatible` for the Lidarr output connector
4. Source connectors in `import` mode are gated (per [CONNECTOR_MANIFEST_v1.md §9](CONNECTOR_MANIFEST_v1.md#9-required-connector-enforcement) — at least one output must be installed for source import mode)
5. Mintarr continues to run; grabs proceed up to verification, then halt at the import phase with a clear "Lidarr version unsupported" message

### 8.2 Mintarr upgrading

Mintarr releases that add support for a new Lidarr major version will be noted in [`CHANGELOG.md`](https://github.com/eivindsjursen-lab/mintarr/blob/main/CHANGELOG.md). The Mintarr version that first supports Lidarr v4 will be tested against both v3.x (reference) and v4.x (whichever stable point).

## 9. Testing against Lidarr

### 9.1 Mock Lidarr in CI

Mintarr's test suite uses mocked Lidarr responses (via `monkeypatch.setattr(server, "_trigger_lidarr_import", ...)`). Tests cover:

- Successful ManualImport
- ManualImport with release-family rejections (force-import path)
- ManualImport with empty candidates (rescue path)
- Rescue path success
- Rescue path failure
- Command polling timeout
- Lidarr unreachable (connection error)

The mocks reflect Lidarr 3.1.x behaviour. When v4 support lands, parallel mocks are added.

### 9.2 Manual testing against live Lidarr

Contributors should test changes touching Mintarr's Lidarr coupling against a real Lidarr instance before merging. The minimum smoke test:

1. Trigger a TIDAL grab (or LocalFolder ingest)
2. Watch the Mintarr logs for the `_trigger_lidarr_import` flow
3. Verify the resulting album appears in Lidarr with correct metadata

A more thorough test exercises the REVIEW_REQUIRED flow:

1. Grab an album where flac-detective returns SUSPICIOUS
2. Verify Mintarr does not import to Lidarr automatically
3. Promote via dashboard
4. Verify the import then succeeds

## 10. Invariants

These hold for Mintarr's Lidarr coupling and are tested:

1. **All Lidarr calls go through the `LidarrClient` interface** (post Phase 4 work). Direct `requests.get(f"{api}/...")` in code outside `app/lidarr/` is a regression.
2. **Lidarr API key never appears in logs.** The key is masked in any log statement that includes the request URL.
3. **Lidarr is treated as semi-trusted** — its responses are validated against expected shapes; unexpected fields are ignored rather than crashing Mintarr.
4. **Lidarr unreachable does not crash Mintarr.** Pipeline phases continue up to and including verify; only the import phase fails with a clear error.
5. **Lidarr version detection is cached.** Mintarr probes `system/status` once per container lifetime, then assumes the version remains stable.

## 11. Future direction

- **Pre-import webhook proposal** ([ADR-0007 §"Alternative 3"](../architecture/adr/0007-no-lidarr-fork.md)) — opportunistic upstream PR to Lidarr adding a `pre-import` event hook that Mintarr can listen on. Replaces the current Newznab+SAB-bridge with a cleaner event-driven model. Pursued only if/when Mintarr reaches the maturity to approach Lidarr maintainers.
- **Lidarr custom-format export** — Mintarr generates Lidarr-importable CF configuration matching the recommended Mintarr CFs. Operator-friendly install path.
- **Lidarr quality-profile validation** — Mintarr inspects the operator's quality profile and warns about settings that defeat verification (e.g., `upgradeAllowed=false` with V2 verification enabled).

These are tracked in [ROADMAP.md](../strategy/ROADMAP.md) Phase 7 (upstream coordination).

---

> Last updated: 2026-05-31
