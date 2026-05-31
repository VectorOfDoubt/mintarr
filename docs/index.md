# Mintarr

**Quality control and import orchestration for Lidarr.**

Mintarr verifies, scores, and orchestrates music imports across multiple sources
(TIDAL, LocalFolder, Soulseek, future SAB / qBit / CD-rip / YouTube) before they
reach Lidarr. Every import is verified, scored, and either auto-imported, sent
for operator review, or blocked — with full audit trail.

Mintarr exposes itself to Lidarr as a Newznab indexer and a SAB-compatible
download client. Existing Lidarr installs adopt Mintarr without configuration
changes on the Lidarr side beyond adding one indexer and one download client.

---

## Start here

| If you want to... | Read |
|---|---|
| Understand what Mintarr is | [Vision](strategy/VISION.md) |
| See what's planned | [Roadmap](strategy/ROADMAP.md) |
| Look up a term | [Glossary](strategy/GLOSSARY.md) |
| Understand the architecture | [Overview](architecture/OVERVIEW.md) |
| Install Mintarr | [Install guide](operations/INSTALL.md) |
| Configure Mintarr | [Configuration reference](operations/CONFIGURATION.md) |
| Build your own adapter | [Adapter tutorial](development/ADAPTER_TUTORIAL.md) |
| Review the decision record | [ADR index](architecture/adr/0001-rename-from-tidalhires.md) |

The full document tracker lives in the
[Mintarr Documentation Index](MINTARR_DOCUMENTATION_INDEX.md).

## What Mintarr verifies

| Verifier | Role |
|---|---|
| `ffprobe` codec gate | Rejects AAC-in-MP4 files mislabelled as FLAC |
| `flac -t` integrity | Rejects FLAC files that don't decode cleanly |
| FLAC Detective spectral analysis | Detects upsampled MP3-into-FLAC and other fake high-resolution audio |
| Future: CUETools / CTDB | CD-rip authenticity verification |
| Future: beets / Picard / AcoustID | Metadata identity (read-only) |

Each verifier produces evidence; the V2 policy turns evidence into one of four
decisions: **ACCEPT** / **ACCEPT_PROVISIONAL** / **REVIEW_REQUIRED** / **BLOCK**.

## What Mintarr is not

Mintarr does **not** manage your music library. That is Lidarr's job. Mintarr
does **not** track wanted / missing albums, talk to MusicBrainz, configure
indexers, write tags, or organise your filesystem. The
[boundary test in ADR-0008](architecture/adr/0008-strategic-positioning.md)
records why and what Mintarr does instead.

## License

Mintarr is licensed under the **GNU Affero General Public License v3.0 only
(AGPL-3.0-only)**. Rationale lives in
[ADR-0005](architecture/adr/0005-license.md).
