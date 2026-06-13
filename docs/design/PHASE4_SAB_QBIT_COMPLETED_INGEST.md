# Phase 4 - SAB/qBittorrent Completed-Folder Ingest

> **Type:** Design document
> **Version:** 1.0 - 2026-06-03
> **Status:** Implemented - `sab_usenet` and `qbittorrent_torrent` source connectors
> **Related:** [ADR-0012 QC import-gate scope](../architecture/adr/0012-qc-import-gate-scope.md), [Download client category gate](DOWNLOAD_CLIENT_CATEGORY_GATE.md), [Mintarr-managed SAB/qBit download-client lane](MINTARR_MANAGED_SAB_QBIT_DOWNLOAD_CLIENT.md), [F3.5 Soulseek completed-folder ingest](F3.5_SOULSEEK_COMPLETED_INGEST.md), [CONNECTOR_MANIFEST_v1.md](../specs/CONNECTOR_MANIFEST_v1.md)

## 1. Goal

Add completed-folder/category ingest source connectors for operator-routed
SABnzbd and qBittorrent output. This extends Mintarr's scoped QC gate to folders
the operator explicitly routes to Mintarr without making Mintarr a universal
gate for everything Lidarr imports.

For automatic Lidarr-triggered SAB/qBit music where Mintarr owns queue status
and import gating, see the proposed successor design
[Mintarr-managed SAB/qBit download-client lane](MINTARR_MANAGED_SAB_QBIT_DOWNLOAD_CLIENT.md).

Implemented connector IDs:

- `sab_usenet`
- `qbittorrent_torrent`

## 2. Scope Boundary

ADR-0012 is binding:

- Mintarr gates only Mintarr-routed sources.
- SAB/qBit completed-folder ingest requires a dedicated Lidarr/Mintarr music
  category and completed path; shared TV/movie completed roots are not valid
  source roots (see [Download client category gate](DOWNLOAD_CLIENT_CATEGORY_GATE.md)).
- These connectors read completed folders, copy into Mintarr-managed work/output,
  then run the normal source-grab pipeline.
- Adapter code never calls Lidarr ManualImport.
- Mintarr does not configure, pause, delete, or otherwise manage SABnzbd or
  qBittorrent.
- Queue cleanup remains Mintarr-owned only: `_cleanup_lidarr_queue()` removes
  rows where `downloadId == jid`; it does not try to own external-client queue
  rows.

## 3. Runtime Shape

Both connectors use the shared `CompletedFolderAdapter` base:

```text
resolve source folder
  -> reject absolute/traversal/symlink paths
  -> reject partial marker files/folders
  -> enforce max file count / max byte count
  -> wait for settle window and compare size/mtime snapshot
copy source files into DOWNLOAD_BASE/<jid>
  -> normalize_audio
  -> verify
  -> prepare OUTPUT_BASE/<jid>
  -> Lidarr ManualImport through common output path
```

This is the Soulseek completed-folder safety model, not the lighter LocalFolder
drop-folder model.

## 4. Configuration

SABnzbd:

| Env var | Default | Meaning |
|---|---|---|
| `SAB_USENET_ENABLED` | `false` | Master toggle for the adapter. |
| `SAB_USENET_DOWNLOAD_ROOT` | unset | Mounted SAB completed/category root. |
| `SAB_USENET_MAX_FILES` | `300` | Maximum files per candidate folder. |
| `SAB_USENET_MAX_BYTES` | `0` | Maximum total bytes, with `0` meaning unlimited. |
| `SAB_USENET_SETTLE_SECONDS` | `10` | Seconds the folder must remain size/mtime stable. |

qBittorrent:

| Env var | Default | Meaning |
|---|---|---|
| `QBITTORRENT_TORRENT_ENABLED` | `false` | Master toggle for the adapter. |
| `QBITTORRENT_TORRENT_DOWNLOAD_ROOT` | unset | Mounted qBittorrent completed/category root. |
| `QBITTORRENT_TORRENT_MAX_FILES` | `300` | Maximum files per candidate folder. |
| `QBITTORRENT_TORRENT_MAX_BYTES` | `0` | Maximum total bytes, with `0` meaning unlimited. |
| `QBITTORRENT_TORRENT_SETTLE_SECONDS` | `10` | Seconds the folder must remain size/mtime stable. |

Both source connectors default to disabled. Per `CONNECTOR_MANIFEST_v1`,
`dry_run` is verifier-only; source connectors are either `disabled` or `import`.

## 5. API

Manual enqueue endpoints:

```text
POST /sab/ingest
POST /qbit/ingest
```

Request:

```json
{"path": "Artist/Album"}
```

`path` is a relative path under the configured completed-download root. Invalid
paths return `400`; partial or unsettled folders return `409`; disabled adapter
or non-import connector mode returns `503`.

## 6. Lidarr Path Mapping

The common import path now uses `MINTARR_LIDARR_COMPLETE_ROOT` for the
Lidarr-visible side of `OUTPUT_BASE/<jid>`.

Default:

```text
MINTARR_LIDARR_COMPLETE_ROOT=/downloads/TidalHiRes/complete
```

This preserves the existing deployment while keeping the path mapping explicit
for Phase 4 operators. Rescue/progress helpers translate Lidarr-visible paths
back to `OUTPUT_BASE` through the same helper.

## 7. Partial Markers

The shared adapter rejects common incomplete markers before copy:

- suffixes: `.!qB`, `.qbt!`, `.part`, `.partial`, `.tmp`, `.download`,
  `.crdownload`, `.incomplete`
- path parts: `__ADMIN__`, `_UNPACK_`, `_FAILED_`

These checks are deliberately conservative. Operators should point Mintarr at a
completed/category output, not an active download directory.

## 8. Tests

Implemented tests cover:

- disabled/enabled conditions for both adapters
- path traversal and symlink blocking
- partial marker rejection for SAB and qBittorrent markers
- settle-window rejection
- copy leaves source folders untouched
- source type threaded into the common pipeline
- `/sab/ingest` and `/qbit/ingest` endpoint behavior
- source connector manifests and import-mode gating
- parameterized Lidarr-visible output path mapping
