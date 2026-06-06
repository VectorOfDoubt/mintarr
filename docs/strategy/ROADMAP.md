# Mintarr — Roadmap

> **Type:** Strategy / live status tracker
> **Version:** 0.1 — 2026-05-26
> **Status:** Living document — phases and statuses update as work lands.
> **Audience:** Anyone wondering "is feature X planned?" or "when does this land?"

---

## How to read this

Mintarr is built without a fixed schedule. Phases are sequenced by dependency, not by date. Each phase has:

- **Goal** — the one-line outcome that defines "done"
- **Status** — `planned` / `in progress` / `shipped`
- **Scope** — concrete deliverables, with links to design docs where they exist
- **Depends on** — what must land first

When a phase ships, this document is updated in the same commit as the merge. Stale phase status is a documentation bug.

For the underlying positioning and what is *not* in any phase, see [VISION.md](VISION.md) and [ADR-0008](../architecture/adr/0008-strategic-positioning.md).

---

## Phase 0 — Open-source foundation

**Goal:** establish Mintarr as a public, documented, contribution-ready project.

**Status:** in progress

**Scope:**

- Rename `tidalhires` → `mintarr` across code, image tags, and documentation
- Start the public repo from a clean initial commit; private monorepo history stays private
- ~~Resolve the AGPL-3.0 vs GPL-3.0 license question raised by the `tidal-dl-ng-For-DJ` dependency~~ — done; AGPL-3.0-only locked ([ADR-0005](../architecture/adr/0005-license.md))
- Create public repository `eivindsjursen-lab/mintarr` (or a successor org) with the cleaned codebase
- Land the P0 documentation set listed in [MINTARR_DOCUMENTATION_INDEX.md §7](../MINTARR_DOCUMENTATION_INDEX.md#7-foundation-pass-what-must-exist-before-phase-0-coding-starts)
- Add GitHub Actions CI (tests, lint, type-check) and container build pipeline
- Add MkDocs Material site deploying to GitHub Pages

**Depends on:** publication audit findings resolved (license decision is now locked — AGPL-3.0-only).

**Not in this phase:** any new runtime feature work. Phase 0 is purely the foundation that makes external contribution possible.

---

## Phase 1 — Connector architecture

**Goal:** every source, verifier, and output is a Connector with a static manifest and a uniform operator surface.

**Status:** partial - F4.1-F4.5 foundation shipped; optional verifier evidence runners remain future work

**Scope:**

- **F4.1 Static connector registry** — `connectors/base.py`, `connectors/registry.py`, manifests for existing integrations (tidal, local_folder, ffprobe, flac_t, flac_detective, lidarr_manual_import, lidarr_rescue_rescan), `GET /dashboard/v1/connectors`
- **F4.2 Integrations dashboard tab** — UI surface for connector status (installed / enabled / mode / health / version / last error / docker service hint)
- **F4.3 Connector enable/disable + dry-run** — `connector_config` table, UI toggles for optional connectors, hard-gate disable protection, audit trail for config changes, `dry_run` vs `import` mode
- **F4.4 Compose profile / install guidance** — implemented ([F4.4 connector install guidance](../design/F4.4_CONNECTOR_INSTALL_GUIDANCE.md)); dashboard shows missing services / mounts / env, documented compose profiles, no app-managed docker updates
- **F4.5 Optional verifier connectors** — implemented registry/design first slice ([F4.5 optional verifier connectors](../design/F4.5_OPTIONAL_VERIFIER_CONNECTORS.md)); Picard/beets/AcoustID metadata-identity verifier defaults disabled, CUETools/CTDB connector for CD-rip lane remains planned, optional sensors start disabled or in `dry_run`

**Depends on:** Phase 0 (so external contributors can author connectors against a stable contract).

**Design references:** [CONNECTOR_PLUGIN_ARCHITECTURE.md](../design/CONNECTOR_PLUGIN_ARCHITECTURE.md), [ADR-0003 Connector vs Adapter](../architecture/adr/0003-connector-vs-adapter.md).

---

## Phase 2 — Operator UI redesign

**Goal:** Mintarr's dashboard feels like an arr-stack tool, with a sidebar layout and Settings / System cards.

**Status:** shipped — sidebar shell with seven sections, vendored Alpine + HTMX (no Node toolchain) per [ADR-0011](../architecture/adr/0011-frontend-framework.md), dark/light/auto theme + table density, live Queue/History/System via HTMX, audit feed with CSV export, and records + topbar search. Remaining Tasks/Logs/Backup/Updates card content and server-side global search are optional follow-ups.

**Scope:**

- Sidebar layout with Overview / Queue / History / Review / Connectors / Settings / System sections
- Topbar with search (jids, artists, albums), notifications, user menu
- Settings cards: General, Source Connectors, Verifier Connectors, Output Connectors, Quality Policies, Notifications, UI
- System cards: Status, Workers, Tasks, Logs, Backup, Updates, Events
- Live worker status view (current job per worker, queue depth, restart action)
- Audit log viewer with filter (level, component, jid) and download
- Dark / light / auto theme switch
- Responsive layout that works on a 375px mobile viewport

**Depends on:** Phase 1 (Connectors drive several of the new cards).

---

## Phase 3 — Observability and integration surface

**Goal:** Mintarr fits cleanly into existing self-hosting observability and notification stacks.

**Status:** shipped — all scope delivered: structured JSON logging (`MINTARR_LOG_FORMAT=json`), Prometheus `/metrics` with a documented metric catalogue, Grafana dashboard templates under `docs/grafana/`, OpenAPI `/openapi.json` + Swagger UI at `/docs`, opt-in Apprise notifications (`MINTARR_NOTIFY_URLS`) on attention events, the backup/restore feature (read-only `GET /backup`, optional scheduled backup zips, and staged restore via `POST /restore` + crash-safe boot-time apply with System-section status/cancel controls), generic webhook-in (`POST /webhook/ingest`), and the MkDocs Material docs site. Enhancements such as Prometheus event counters/histograms and a richer event-metric surface are tracked as future work, not Phase 3 scope.

**Scope:**

- Structured JSON logging with stable field names
- Prometheus `/metrics` endpoint with documented metric catalogue
- Grafana dashboard templates published under `docs/grafana/`
- OpenAPI specification, served at `/openapi.json` with Swagger UI at `/docs`
- Apprise-based notification connector for outbound alerts (Telegram, Discord, Pushover, ntfy, Slack, Matrix, etc.)
- Webhook-in endpoint for external triggers (n8n, IFTTT, custom scripts)
- Backup / restore as first-class feature: scheduled backups, restore UI, sidecar + state_db export to zip
- MkDocs Material documentation site with full-text search

Restore implementation follows [Phase 3 restore endpoint design](../design/PHASE3_RESTORE_ENDPOINT_DESIGN.md): stage via endpoint, apply on restart before workers start, and never live-overwrite state in a running process.

**Depends on:** Phase 1 (Connectors define what monitoring needs to surface).

---

## Phase 4 — Source coverage

**Goal:** Mintarr supports the realistic source mix of a self-hosted music collector.

**Status:** partial (TIDAL, LocalFolder, Soulseek, SAB completed-folder, and qBittorrent completed-folder shipped)

**Scope:**

- **F3.5a Soulseek/slskd completed-folder ingest** — implemented ([F3.5a completed-folder ingest design](../design/F3.5_SOULSEEK_COMPLETED_INGEST.md))
- **F3.5b Soulseek/slskd HTTP search and download** — implemented ([F3.5B Soulseek slskd trigger](../design/F3.5B_SOULSEEK_SLSKD_TRIGGER.md))
- **SAB/qBit completed-category ingest** — implemented as `sab_usenet` and `qbittorrent_torrent` completed-folder source connectors ([Phase 4 SAB/qBit completed ingest](../design/PHASE4_SAB_QBIT_COMPLETED_INGEST.md)). Scope is locked by [ADR-0012](../architecture/adr/0012-qc-import-gate-scope.md): Mintarr gates operator-routed completed folders; it does not manage download clients, and the universal "QC everything Lidarr imports" gate is a deferred future phase.
- **CD-rip / private-tracker torrent evidence lane** — design pending; depends on Phase 1 connector model
- **YouTube fallback connector (Tubifarry-style)** — design pending; explicitly tagged as never-an-upgrade-source

**Depends on:** Phase 1 for new connectors to slot into the registry cleanly.

---

## Phase 5 — Quality refinement

**Goal:** Mintarr's verification and import decisions reflect the real-world ambiguity of music release data.

**Status:** partial — F5.1 shipped; F5.3 local CD-rip evidence shipped (advisory sensor + opt-in default-off scoring), network CTDB/AccurateRip cross-check pending its own design beat; F5.2 planned

**Scope:**

- **F5.1 Release-family matching** — **shipped**, architecture locked in [ADR-0013](../architecture/adr/0013-release-family-identity-policy.md). Mintarr-side mitigation of Lidarr's multi-album / edition / deluxe / remaster / anniversary matching weakness, built as a separate release-identity policy axis (two-axis audio/identity, where good audio never excuses the wrong album): Lidarr-first expected metadata with MusicBrainz/MBID as advisory evidence, read-only `mutagen` tag evidence, confidence + abstain (weak metadata → review, never block), dashboard visibility, and an opt-in **default-off** release-switch strategy with operator review-mode override (snapshot + audit + restore, same-album-only, never on `WRONG_ALBUM`/audio-`BLOCK`). The locked alternative-to-fork mechanism from [ADR-0007 §"Multi-album / release matching is not a fork problem"](../architecture/adr/0007-no-lidarr-fork.md).
- **F5.2 Source-aware verification thresholds** — per-source confidence weighting (e.g., Soulseek requires stricter spectral pass than TIDAL); not source-aware policy until evidence justifies it
- **F5.3 CD-rip evidence integration** — operational design in [F5.3 CD-rip evidence lane](../design/F5.3_CD_RIP_EVIDENCE_LANE.md). **Shipped:** read-only rip-log/cue parsing, an advisory `cd_rip_evidence` sensor surfaced in the dashboard, and opt-in **default-off** decision scoring (`MINTARR_CD_RIP_SCORING`) per §5.4 (AccurateRip-verified rip can lift a borderline review; log-backed mismatch routes to review; never overrides a hard gate or identity `WRONG_ALBUM`). **Pending:** the network CTDB/AccurateRip online cross-check verifier connector — designed in [F5.3B CTDB network verifier](../design/F5.3B_CTDB_NETWORK_VERIFIER.md) (default-off; disc-ID lookup before checksum recompute), not yet implemented.

**Depends on:** Phase 1 connector model; Phase 4 source coverage.

---

## Phase 6 — Output diversification

**Goal:** Mintarr is useful to operators who do not run Lidarr.

**Status:** planned (post-V1.0)

**Scope:**

- Plex direct-import OutputConnector
- Jellyfin direct-import OutputConnector
- Filesystem-only OutputConnector (move verified files to a configured library path with no downstream manager)
- Multi-output routing (same import can target multiple outputs based on policy)

**Depends on:** Phase 1 connector model (OutputConnector is already a first-class concept).

---

## Phase 7 — Upstream coordination (opportunistic)

**Goal:** reduce Mintarr's Lidarr dependency from "always required" to "primary output among several".

**Status:** opportunistic — pursued when one of the conditions in [ADR-0007 §Re-evaluation triggers](../architecture/adr/0007-no-lidarr-fork.md) is approached.

**Scope:**

- Pre-import webhook PR to Lidarr upstream (if upstream maintainers are receptive)
- Lidarr v4 client implementation alongside v3 client (capability-detection at boot)
- Coordination with Lidarr maintainers on Custom Format conventions for `[TIDAL]`, `[Local]`, `[Soulseek]` source tags

**Depends on:** Mintarr v1.0 release (adoption signal before approaching upstream).

---

## Phase 8 — Mintarr Audio Lab (additive sensors)

**Goal:** Mintarr ships its own audio analysis sensors that complement (not replace) the external verifier stack.

**Status:** intended direction. Not scoped; not started. Pursued only after Phase 4 source coverage and Phase 5 quality refinement have produced clear gaps that in-house sensors would fill.

**Scope (tentative — locked when an F-numbered design doc lands):**

- **DR meter sensor** — Dynamic Range computation, comparison against DR-Database baselines
- **Loudness sensor** — EBU R128 / ReplayGain analysis as first-class evidence
- **Transient sensor** — perceptual transient quality analysis (compression / re-encoding heuristic complementary to spectral cutoff)
- **Codec entropy sensor** — second-opinion authenticity signal independent of spectral analysis
- **AccurateRip CRC sensor** (CD-rip lane) — cross-reference with CTDB / AccurateRip databases for CD-rip authenticity proof beyond what CUETools already does

**What this phase is NOT:**

- A FLAC Detective replacement. FLAC Detective remains the primary spectral verifier. Phase 8 sensors are **additive evidence**, not substitutes. Replacing FLAC Detective is a separate decision documented in a future ADR if/when it becomes appropriate.
- A general-purpose audio analysis library. Mintarr Audio Lab sensors exist to feed the V2 verification policy; they are not standalone tools.
- Tag writing or library reorganisation. Out of scope per [ADR-0008](../architecture/adr/0008-strategic-positioning.md) boundary test.

**Depends on:**

- Phase 4.5 (F4.5) optional verifier connectors must be operational — Audio Lab sensors plug into the same connector architecture
- Real operator feedback identifying what existing verifiers miss
- DSP-aware contributor (maintainer team is not currently audio-DSP-expert)

**Re-evaluation triggers:**

- Operator demand for sensors not covered by FLAC Detective, CTDB, beets/Picard/AcoustID
- FLAC Detective project changes status (unmaintained, license incompatibility, etc.)

---

## Cross-cutting concerns

Some work spans phases and is tracked here rather than in a specific phase:

| Concern | Approach |
|---|---|
| Multi-Lidarr-version support | `lidarr/v1/client.py`, capability probing at boot, mock fixtures in CI, designed in Phase 0 specs, exercised in every phase |
| API versioning | SemVer on `SourceAdapter`, `ConnectorManifest`, `SidecarFormat`, HTTP API; each version is a separate spec document, [ADR-0004](../architecture/adr/0004-api-versioning-semver.md) |
| Security | Threat model in [SECURITY_MODEL.md](../architecture/SECURITY_MODEL.md), public vulnerability disclosure via [SECURITY.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/SECURITY.md), no Docker socket mount, secrets never browser-visible |
| Documentation | Every phase ships docs as part of the deliverable. Design docs use the F-number convention. ADRs cover locked decisions. Operator-facing docs follow the layout in [MINTARR_DOCUMENTATION_INDEX.md](../MINTARR_DOCUMENTATION_INDEX.md). |

---

## What is intentionally not on the roadmap

These have been considered and rejected. They are listed so contributors do not propose them expecting they are "missing":

- Forking Lidarr ([ADR-0007](../architecture/adr/0007-no-lidarr-fork.md))
- Owning artist / album library state ([ADR-0008 boundary test](../architecture/adr/0008-strategic-positioning.md))
- MusicBrainz model integration as first-class data
- Tag writing (out of scope until separate ADR establishes Lidarr tag-ownership boundaries)
- Multi-tenant / multi-user / RBAC roles ([ADR-0002 single-instance arr-pattern](../architecture/adr/0002-single-instance-arr-pattern.md))
- Dynamic third-party plugin loading from UI (security surface; static connector registry first per [Connector architecture §3](../design/CONNECTOR_PLUGIN_ARCHITECTURE.md))
- Docker socket mounted into Mintarr container (security)

If any of these become viable, a new ADR overrides the old one. Until then, contributions in these areas will be closed with a reference to the relevant ADR.

---

> Last updated: 2026-06-05
