# Download Client Category Gate

> **Type:** Design document
> **Version:** 0.1 - 2026-06-10
> **Status:** Proposed
> **Related:** [ADR-0012 QC import-gate scope](../architecture/adr/0012-qc-import-gate-scope.md), [Phase 4 SAB/qBit completed ingest](PHASE4_SAB_QBIT_COMPLETED_INGEST.md), [Lidarr integration](../specs/LIDARR_INTEGRATION.md)

## 1. Problem

Mintarr can ingest completed folders from SABnzbd and qBittorrent through the
`sab_usenet` and `qbittorrent_torrent` source connectors. That mechanism is safe
only when the completed folder is a Mintarr-routed music lane.

Real Arr deployments usually share download clients:

- Sonarr uses SAB/qBit for TV.
- Radarr uses SAB/qBit for movies.
- Lidarr may use the same clients for music.
- Operators often have broad completed folders such as `complete/`,
  `downloads/`, or `ferdig/` that contain mixed content.

If Mintarr watches a shared completed folder, it can accidentally inspect,
copy, or enqueue non-music content that belongs to another Arr application.
That would violate [ADR-0012](../architecture/adr/0012-qc-import-gate-scope.md):
Mintarr is the QC gate for Mintarr-routed sources, not a universal proxy for
every download client item.

The local dogfood environment confirmed this risk: SAB's completed directory
was empty, while qBittorrent's completed directory contained TV/movie content in
a flat shared folder. Dogfooding the SAB/qBit lane there would test the wrong
topology.

## 2. Decision

Mintarr requires a **dedicated Lidarr/Mintarr music category** for SAB/qBit
completed-folder ingest.

Mintarr must not treat a generic download-client completed directory as a valid
source root. A SAB/qBit source connector is eligible only when the operator has
explicitly configured:

1. a dedicated music category in the download client, and
2. a completed path for that category, and
3. a Mintarr source connector pointed at that category's completed path.

In short:

```text
Sonarr/Radarr categories  -> stay owned by Sonarr/Radarr
Lidarr/Mintarr category   -> dedicated completed folder -> Mintarr QC -> Lidarr import
```

Mintarr owns the quality/import path only after the operator routes the category
to Mintarr. It does not configure SAB/qBit, does not manage unrelated
categories, and does not scan shared completed roots.

One integration problem remains deliberately explicit: if Lidarr is configured
to grab through a normal SAB/qBit download client, Lidarr also polls that client
and may auto-import completed items from the same category. A completed-folder
category is therefore not enough by itself. The operator must also make the
import lifecycle Mintarr-owned for that lane. The exact safe setup for "Lidarr
grabs through SAB/qBit, but Mintarr imports" is still an open design question
unless Lidarr is prevented from independently importing the category. Until that
mechanism is specified and tested, SAB/qBit completed-folder ingest should be
treated as operator-routed ingest, not as automatic interception of Lidarr's own
external-client grabs.

## 3. Required Topology

### 3.1 Good topology

```text
qBittorrent
  categories:
    tv             -> /downloads/qbit/tv
    movies         -> /downloads/qbit/movies
    lidarr-music   -> /downloads/qbit/lidarr-music

Mintarr
  QBITTORRENT_TORRENT_DOWNLOAD_ROOT=/downloads/qbit/lidarr-music
  QBITTORRENT_TORRENT_ENABLED=true

Lidarr
  uses the music category/path intended for Mintarr-routed import
```

SABnzbd follows the same shape:

```text
SABnzbd
  categories:
    tv             -> /downloads/sab/tv
    movies         -> /downloads/sab/movies
    lidarr-music   -> /downloads/sab/lidarr-music

Mintarr
  SAB_USENET_DOWNLOAD_ROOT=/downloads/sab/lidarr-music
  SAB_USENET_ENABLED=true
```

### 3.2 Bad topology

```text
qBittorrent
  all categories -> /downloads/qbit/ferdig

Mintarr
  QBITTORRENT_TORRENT_DOWNLOAD_ROOT=/downloads/qbit/ferdig
```

This is rejected as an operator topology, even if the connector's path safety
checks can technically contain the filesystem access. The problem is ownership,
not path traversal: the folder is shared by other Arr applications.

## 4. Operator Contract

Operators who enable `sab_usenet` or `qbittorrent_torrent` must ensure:

- the watched root is music-only;
- non-Lidarr categories do not write into the watched root;
- Lidarr does not independently import from the same completed folder while
  Mintarr is expected to gate it. This is the hardest part of the integration
  and must be demonstrated before claiming SAB/qBit grabs are fully gated;
- Mintarr's access to the source root is read-only where the deployment allows
  it; read-write mounts are discouraged and should be surfaced as warnings;
- Mintarr's output path is visible to Lidarr via `MINTARR_LIDARR_COMPLETE_ROOT`.

Mintarr should phrase this as a gating contract in the UI:

> This connector only gates downloads routed to the configured music category.
> Shared TV/movie completed folders are not supported.

## 5. Discovery And Onboarding

Lidarr download-client discovery is useful, but it is **advisory**. It can help
operators find likely categories and paths; it must not imply that Mintarr is
automatically gating every Lidarr download.

### 5.1 Inputs

Mintarr may read:

- `GET /api/v1/downloadclient`
- `GET /api/v1/remotepathmapping`

The download-client response can expose categories and paths inside
client-specific `fields[]`. Remote path mappings translate client-side paths to
Lidarr-visible paths. Neither endpoint proves that a folder is music-only.

There are three distinct path namespaces:

| Namespace | Example | Source of truth |
|---|---|---|
| Download-client path | `/downloads/qbit/lidarr-music` | SAB/qBit category configuration |
| Lidarr-visible path | `/data/downloads/qbit/lidarr-music` | Lidarr remote path mappings |
| Mintarr container path | `/mnt/qbit/lidarr-music` | Operator's Mintarr container mount |

Mintarr can infer the first two from Lidarr's API only best-effort. It cannot
derive its own container mount path from Lidarr; the operator must configure
the Mintarr mount/root explicitly.

### 5.2 Onboarding flow

Recommended dashboard flow:

1. Read Lidarr download clients.
2. Show candidate SAB/qBit clients and any discovered categories.
3. Ask the operator to select or type the dedicated music category.
4. Resolve the completed path for that category.
5. Apply remote-path mapping if needed.
6. Run validation checks.
7. Show a dry-run summary.
8. Enable the connector only after explicit confirmation.

Example dry-run:

```text
Connector: qbittorrent_torrent
Category: lidarr-music
Client path: /downloads/qbit/lidarr-music
Mintarr mount: /mnt/qbit/lidarr-music
Lidarr-visible output: /downloads/TidalHiRes/complete/<jid>
Result: eligible
Coverage: only qBittorrent items in category "lidarr-music"
```

## 6. Validation Rules

Validation should be conservative.

### 6.1 Hard failures

Fail onboarding if:

- no category is configured;
- no completed path is configured;
- the configured root is `/`, a home directory, or another broad unsafe root;
- the root is not mounted inside the Mintarr container;
- the root escapes its configured mount after realpath resolution;
- the configured path is exactly the same as another category path discovered
  from the same Lidarr download-client configuration, because category ownership
  is not isolated.

### 6.2 Warnings

Warn, but allow explicit override, if:

- the folder is empty, so Mintarr cannot prove the category shape yet;
- the download-client API does not expose category-specific paths;
- remote path mappings are ambiguous;
- the same physical path appears in multiple download-client categories;
- the category name is generic, such as `complete`, `downloads`, or `finished`.
- the root is mounted read-write rather than read-only;
- dry-run sampling finds obvious non-music media at the top level. Sampling is a
  heuristic: a music release can contain bonus video, artwork, or other extras,
  so this is not a hard failure by itself;
- the path looks like a shared TV/movie root by naming or sampled contents.
  Mintarr usually cannot query Sonarr/Radarr, so this is a heuristic warning
  unless the same path is also discovered in Lidarr's own client configuration.

### 6.3 Runtime rejection

At runtime, completed-folder ingest still enforces the existing safety model:

- relative candidate path only;
- traversal rejection;
- symlink rejection;
- partial marker rejection;
- settle window;
- max file count / max byte count;
- copy into Mintarr-managed work/output before QC.

Those checks are necessary but not sufficient. Category ownership is validated
at onboarding/config level.

## 7. Configuration Model

Current connector env vars remain valid:

```text
SAB_USENET_ENABLED=false
SAB_USENET_DOWNLOAD_ROOT=

QBITTORRENT_TORRENT_ENABLED=false
QBITTORRENT_TORRENT_DOWNLOAD_ROOT=
```

Future onboarding should persist or expose category intent explicitly, for
example:

```text
SAB_USENET_CATEGORY=lidarr-music
QBITTORRENT_TORRENT_CATEGORY=lidarr-music
```

The category value is not just cosmetic. It documents the operator's routing
contract and gives the dashboard a stable label for warnings and coverage.

## 8. Coverage Semantics

The dashboard must distinguish:

| State | Meaning |
|---|---|
| `disabled` | Connector is off; nothing from this client is gated. |
| `configured` | Category/path are configured but not yet validated with live data. |
| `eligible` | Dedicated music category/path passed validation. |
| `watching` | Connector is enabled and ready to ingest operator-routed items. |
| `shared_path_warning` | Path appears shared; connector should stay disabled. |
| `invalid` | Path/category cannot be used safely. |

The UI should avoid blanket language such as "qBittorrent is protected".
Correct wording:

> qBittorrent category `lidarr-music` is routed through Mintarr.

Incorrect wording:

> qBittorrent is routed through Mintarr.

## 9. Dogfood Guidance

Do not dogfood SAB/qBit completed-folder ingest with an artificial folder that
does not match the real download-client topology.

A meaningful dogfood requires:

- a real SAB/qBit category dedicated to music;
- a real completed path for that category;
- Lidarr configured so the relevant music grabs land there;
- Mintarr configured against that same category path;
- at least one real music release routed through the category.

If the environment only has shared TV/movie folders, dogfood should stop at:

- config discovery;
- dry-run validation;
- expected rejection/warning of the shared path.

This is a valid outcome. It proves the guardrail works.

## 10. Implementation Slices

### Slice 1 - Spec and operator docs

- Add this design document.
- Cross-link from Phase 4 completed-folder ingest docs.
- Document the dedicated-category requirement in configuration/install docs.

### Slice 2 - Read-only discovery

- Add Lidarr client helpers for `/downloadclient` and `/remotepathmapping`.
- Parse SAB/qBit category/path fields best-effort.
- Store no secrets in logs or sidecars.
- Represent the three path namespaces explicitly: client path, Lidarr-visible
  path, and Mintarr container path.

### Slice 3 - Validation/dry-run

- Add a dry-run endpoint that evaluates a proposed category/path.
- Return structured findings: `eligible`, `warning`, `invalid`.
- Include sampled path-shape observations without leaking full host paths.
- Treat shared-folder and non-music-content detection as warnings unless the
  ownership conflict is proven by configuration.

### Slice 3b - Lidarr auto-import safety design

- Specify how an operator prevents Lidarr from independently importing the same
  SAB/qBit category while Mintarr is expected to be the import gate.
- Document supported setups and unsupported setups.
- Do not claim automatic SAB/qBit grab coverage until this lifecycle ownership
  is demonstrated against a live Lidarr + SAB/qBit setup.

### Slice 4 - Dashboard onboarding

- Add a connector setup panel that shows coverage semantics.
- Require explicit operator confirmation before enabling.
- Make shared-folder warnings prominent.

### Slice 5 - Runtime guardrails

- Include category intent in connector status.
- Refuse enabled source connector startup when required category/path validation
  has failed.
- Keep completed-folder runtime checks unchanged and shared.

## 11. Non-goals

- Mintarr does not configure SABnzbd or qBittorrent.
- Mintarr does not migrate existing Sonarr/Radarr categories.
- Mintarr does not scan generic completed folders looking for music.
- Mintarr does not guarantee that Lidarr's own direct grabs are QC-gated unless
  the operator routes that category to Mintarr.
- Mintarr does not become a universal download-client proxy in this design.

## 12. Review Focus

Claude/reviewer should check:

1. Does this keep ADR-0012's scoped-gate boundary intact?
2. Are the hard failures too strict or not strict enough?
3. Is category ownership explicit enough for real shared Arr deployments?
4. Does the onboarding flow avoid promising universal coverage?
5. Are there any SAB/qBit category/path realities this document misses?
6. Is the Lidarr auto-import ownership question explicit enough, or should it
   become a separate ADR before any broader SAB/qBit dogfood?
