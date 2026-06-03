# Use Cases

> **Type:** Community / orientation
> **Version:** 0.1 - 2026-06-03
> **Status:** Draft skeleton. Grows from real deployments and operator reports.
> **Audience:** Operators deciding whether Mintarr fits their workflow.

---

## 1. Why this document exists

Mintarr is not for every music library. This document describes the operator
profiles Mintarr is designed for and the workflows it deliberately does not try
to own.

For the formal scope boundary, see [VISION.md](../strategy/VISION.md) and
[ADR-0008](../architecture/adr/0008-strategic-positioning.md).

## 2. The self-hosted Lidarr operator

You already run Lidarr and want better pre-import quality control.

Typical setup:

- Lidarr tracks wanted/missing albums.
- Mintarr is configured as a Newznab indexer and SAB-compatible download client.
- Mintarr source adapters return candidates to Lidarr.
- Mintarr verifies grabbed files before Lidarr imports them.

Why Mintarr helps:

- catches non-FLAC files advertised as FLAC
- runs FLAC integrity checks
- runs FLAC Detective before import
- keeps sidecars explaining every decision

This is Mintarr's primary v1 use case.

## 3. The multi-source collector

You use more than one source path: TIDAL, LocalFolder, Soulseek, and future
SAB/qBit completed categories.

Without Mintarr, each source tends to grow its own scripts and exceptions. With
Mintarr, every source enters the same four-phase pipeline:

1. download or copy raw files
2. normalize audio into Mintarr's work area
3. verify quality and authenticity
4. import through Lidarr

Why Mintarr helps:

- one verification policy across sources
- one audit trail
- one dashboard
- source adapters remain copy-only and do not mutate source folders

## 4. The Soulseek quality-control operator

You want Soulseek coverage but do not fully trust peer-supplied files.

Mintarr can expose Soulseek candidates through Lidarr search using slskd, queue
selected files, wait for them to land, copy them, verify them, and then import.

Why Mintarr helps:

- blocks fake or corrupted FLAC before import
- guards against obvious wrong-album ManualImport resolutions
- leaves Soulseek source folders untouched

Mintarr is not a Soulseek client; slskd remains the Soulseek client.

## 5. The local-staging operator

You sometimes acquire albums outside Lidarr and want to stage them safely before
import.

LocalFolder ingest lets you place a folder under a mounted ingest root and ask
Mintarr to copy it into the normal pipeline.

Why Mintarr helps:

- avoids manual imports that skip verification
- rejects traversal/symlink escape paths
- records the same sidecar evidence as other sources

## 6. The review-queue operator

You want ambiguous albums held for review instead of auto-imported or deleted.

Mintarr's V2 policy can mark records as `REVIEW_REQUIRED` when evidence is not
safe enough for automatic import but not severe enough for a hard block.

Why Mintarr helps:

- separates hard failures from operator decisions
- keeps review evidence in sidecars
- supports promote/discard workflows

## 7. When Mintarr is probably not worth it

Mintarr may be unnecessary if:

- you do not care about FLAC authenticity
- you only use trusted indexers and accept Lidarr's import decisions
- you grab very few albums and prefer manual inspection
- you do not run Lidarr and do not want a companion service
- you want Mintarr to tag, rename, or reorganize your library

Tag writing and library organization are intentionally out of scope.

## 8. Use cases still being explored

These are plausible but not yet first-class v1 workflows:

- CD-rip evidence with CUETools/CTDB
- read-only Beets/Picard/AcoustID verifier connectors
- Prometheus/Grafana observability
- non-Lidarr output connectors such as Plex, Jellyfin, or filesystem-only output
- in-house additive audio sensors

Track [ROADMAP.md](../strategy/ROADMAP.md) for phase ordering.

---

> Last updated: 2026-06-03
