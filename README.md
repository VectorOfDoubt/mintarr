# Mintarr

> Quality control and import orchestration for Lidarr.
> Verifies, scores, and orchestrates music imports across multiple sources.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Documentation](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://eivindsjursen-lab.github.io/mintarr/)
[![Status](https://img.shields.io/badge/status-foundation--phase-orange)](docs/strategy/ROADMAP.md)

---

## What Mintarr is

**Mintarr is the quality control and import orchestration layer that Lidarr lacks. It is not a better Lidarr.**

Mintarr sits between music sources (TIDAL, LocalFolder, Soulseek, future SAB / qBit / CD-rip / YouTube) and Lidarr. Every import is verified, scored, and either auto-imported, sent for operator review, or blocked — with full audit trail.

Mintarr exposes itself to Lidarr as a Newznab indexer and a SAB-compatible download client. Existing Lidarr installs adopt Mintarr without configuration changes on the Lidarr side beyond adding one indexer and one download client.

For the longer-form vision, read [docs/strategy/VISION.md](docs/strategy/VISION.md).

## What Mintarr verifies

| Verifier | Role |
|---|---|
| `ffprobe` codec gate | Rejects AAC-in-MP4 files mislabelled as FLAC |
| `flac -t` integrity | Rejects FLAC files that don't decode cleanly |
| FLAC Detective spectral analysis | Detects upsampled MP3-into-FLAC and other fake high-resolution audio |
| Future: CUETools / CTDB | CD-rip authenticity verification |
| Future: beets / Picard / AcoustID | Metadata identity (read-only) |

Each verifier produces evidence; the V2 policy turns evidence into one of four decisions: **ACCEPT** / **ACCEPT_PROVISIONAL** / **REVIEW_REQUIRED** / **BLOCK**.

## Status

Mintarr is in its **open-source foundation phase** (Phase 0 of the [roadmap](docs/strategy/ROADMAP.md)). The codebase that became Mintarr has been running in production for several months as `tidalhires`; the rename and the public-repository cutover are documented in [ADR-0001](docs/architecture/adr/0001-rename-from-tidalhires.md).

There is **no public release yet**. The public repository (`eivindsjursen-lab/mintarr`) is being prepared. Watch the [roadmap](docs/strategy/ROADMAP.md) for the cutover announcement.

## Quick start

> Quick start instructions are placeholder until Phase 0 cutover completes. See [docs/operations/INSTALL.md](docs/operations/INSTALL.md) for the current state.

```yaml
# docker-compose.yml (preview — subject to change)
services:
  mintarr:
    image: ghcr.io/eivindsjursen-lab/mintarr:latest
    container_name: mintarr
    ports:
      - "127.0.0.1:5025:8000"
    volumes:
      - ./config:/config
      - /path/to/lidarr/config:/lidarr-config:ro
    environment:
      - MINTARR_API_KEY=<generate-something-long>
      - LIDARR_API_URL=http://lidarr:8686/api/v1
```

Then in Lidarr:

1. Settings → Indexers → Add → Newznab → URL `http://mintarr:8000`, API key matches
2. Settings → Download Clients → Add → SABnzbd → URL `http://mintarr:8000`, API key matches

## Documentation

Full documentation is on the [Mintarr docs site](https://eivindsjursen-lab.github.io/mintarr/) (deploys at Phase 0 cutover). Source lives under [`docs/`](docs/).

| If you want to... | Read |
|---|---|
| Understand what Mintarr is | [VISION.md](docs/strategy/VISION.md) |
| See what's planned | [ROADMAP.md](docs/strategy/ROADMAP.md) |
| Understand the architecture | [OVERVIEW.md](docs/architecture/OVERVIEW.md) |
| Look up a term | [GLOSSARY.md](docs/strategy/GLOSSARY.md) |
| Install Mintarr | [INSTALL.md](docs/operations/INSTALL.md) |
| Configure Mintarr | [CONFIGURATION.md](docs/operations/CONFIGURATION.md) |
| Contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Build your own adapter | [ADAPTER_TUTORIAL.md](docs/development/ADAPTER_TUTORIAL.md) |
| Report a security issue | [SECURITY.md](SECURITY.md) |

## What Mintarr is not

Mintarr does **not** manage your music library. That is Lidarr's job. Mintarr does **not** track wanted / missing albums, talk to MusicBrainz, configure indexers, write tags, or organise your filesystem. The [boundary test in ADR-0008](docs/architecture/adr/0008-strategic-positioning.md) records why and what Mintarr does instead.

If you wanted a Lidarr replacement, Mintarr is not it. If you wanted a tag editor, Mintarr is not it. If you wanted to bypass streaming-service DRM, Mintarr is not it.

If you wanted a quality control and orchestration layer that catches fake high-resolution audio before it reaches your library, you're in the right place.

## Contributing

Mintarr is built for community contribution. The [Documentation Index](docs/MINTARR_DOCUMENTATION_INDEX.md) tracks what is planned, what is drafted, and what is locked.

Before opening a PR:

1. Read [VISION.md](docs/strategy/VISION.md) and [ADR-0008](docs/architecture/adr/0008-strategic-positioning.md) to understand scope
2. Read [CONTRIBUTING.md](CONTRIBUTING.md) for PR process
3. Run the boundary test from ADR-0008 against your proposed change
4. Open an issue first for non-trivial changes — fast scope feedback saves wasted work

Adapter authors should read [ADAPTER_PROTOCOL_v1.md](docs/specs/ADAPTER_PROTOCOL_v1.md) and the [ADAPTER_TUTORIAL.md](docs/development/ADAPTER_TUTORIAL.md).

## License

Mintarr is licensed under the **GNU Affero General Public License v3.0 only (AGPL-3.0-only)**. See [`LICENSE`](LICENSE) for the full text and [`ADR-0005`](docs/architecture/adr/0005-license.md) for the rationale.

The AGPL-3.0 §13 "Remote Network Interaction" clause applies: if you run Mintarr as a service that users interact with over a network, you must offer them the corresponding source on request. For private/single-user self-hosting this typically means linking to the upstream repository; for hosted deployments serving multiple users it means publishing your modified source.

## Acknowledgements

Mintarr inherits design conventions from the broader arr-stack (Sonarr, Lidarr, Radarr, Prowlarr). It uses Radexito's `tidal-dl-ng-For-DJ` fork for TIDAL fetching and `flac-detective` for spectral analysis. Documentation tooling is [MkDocs Material](https://squidfunk.github.io/mkdocs-material/).

The project was previously named `tidalhires`. Migration notes are in [docs/operations/UPGRADE_GUIDE.md](docs/operations/UPGRADE_GUIDE.md).
