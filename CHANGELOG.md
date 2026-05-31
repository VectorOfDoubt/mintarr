# Changelog

All notable changes to Mintarr are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Foundation phase

Mintarr is in its open-source foundation phase. No releases yet.

The pre-rename project (`tidalhires`) ran in private production through F1-F4 design work. The history of those phases is preserved in the design documents under [`docs/design/`](docs/design/) but is not part of any Mintarr release.

Phase 0 progress is tracked in [docs/strategy/ROADMAP.md](docs/strategy/ROADMAP.md).

### Planned for first release (v0.1.0)

- Static connector registry (F4.1)
- Renamed code (`tidalhires/` → `mintarr/`)
- AGPL-3.0-only license ([LICENSE](LICENSE), [ADR-0005](docs/architecture/adr/0005-license.md))
- Sanitised public repository at `eivindsjursen-lab/mintarr`
- MkDocs Material documentation site at `<org>.github.io/mintarr`
- GitHub Actions CI for tests, lint, type-check
- Container images published to GitHub Container Registry on release tags

---

## Pre-rename history (`tidalhires`)

The codebase that became Mintarr previously shipped under the name `tidalhires` in the private `eivindsjursen-lab/sjursen-mediastack` monorepo. Major milestones from that period:

- **F1** SQLite state index (records, sensor_runs, file_evidence, actions, jobs)
- **F2** Worker queue (SQLite-backed, lease + heartbeat, retry policy)
- **F3.1** Source adapter abstraction + TIDAL extract
- **F3.2/F3.3** Multi-adapter Newznab + addurl routing
- **F3.4** LocalFolderAdapter
- **F3.5** Soulseek/slskd adapter (design locked; implementation deferred)
- **F4** Connector / plugin architecture (design)

These milestones are documented in the design files under [`docs/design/`](docs/design/). They are not Mintarr releases; the Mintarr release line starts at v0.1.0.

---

> Last updated: 2026-05-26
