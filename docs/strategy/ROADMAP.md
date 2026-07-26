# Mintarr — Roadmap

> **Type:** Strategy / live status tracker
> **Version:** 0.1 — 2026-05-26
> **Status:** PAUSED INDEFINITELY — 2026-07-26
> **Audience:** Anyone wondering "is feature X planned?" or "when does this land?"

This roadmap is retained as historical planning context. It is not an active
delivery commitment. See [PAUSE_2026-07-26.md](PAUSE_2026-07-26.md) for the
owner decision and resume gate.

---

## How to read this

Mintarr is built without a fixed schedule. Phases are sequenced by dependency, not by date. Each phase has:

- **Goal** — the one-line outcome that defines "done"
- **Status** — `planned` / `in progress` / `shipped`
- **Scope** — concrete deliverables, with links to design docs where they exist
- **Depends on** — what must land first

All phase statuses below are frozen snapshots from the pause date, not current
work indicators. If the project is resumed, this document must be reviewed and
updated in the same change that clears the pause gate.

For the underlying positioning and what is *not* in any phase, see [VISION.md](VISION.md) and [ADR-0008](../architecture/adr/0008-strategic-positioning.md).

---

## Phase 0 — Open-source foundation

**Goal:** establish Mintarr as a public, documented, contribution-ready project.

**Historical status at pause:** in progress

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

**Historical status at pause:** partial - F4.1-F4.5 foundation shipped; optional verifier evidence runners remained future work

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

**Historical status at pause:** shipped — sidebar shell with seven sections, vendored Alpine + HTMX (no Node toolchain) per [ADR-0011](../architecture/adr/0011-frontend-framework.md), dark/light/auto theme + table density, live Queue/History/System via HTMX, audit feed with CSV export, and records + topbar search. Remaining Tasks/Logs/Backup/Updates card content and server-side global search were optional follow-ups.

**Scope:**

- Sidebar layout with Overview / Queue / History / Review / Connectors / Settings / System sections
- Topbar with search (jids, artists, albums), notifications, user menu
- Settings cards: General, Source Connectors, Verifier Connectors, Output Connectors, Quality Policies, Notifications, UI
- System cards: Status, Workers, Tasks, Logs, Backup, Updates, Events
- Live worker status view (current job per worker, queue depth, restart action)
- Audit log viewer with filter (level, component, jid) and download
- Dark / light / auto theme switch
- Responsive layout that works on a 375px mobile viewport

**Future operator controls:**

- **Lidarr blocklist visibility / controlled unblock.** Lidarr remains the
  authoritative blocklist. Mintarr should surface releases it caused Lidarr to
  blocklist, with reason, originating record, actor, timestamp, and exact
  release/download id where available. Mintarr-only controls such as clearing an
  album hold are already safe because they touch Mintarr state only. A Lidarr
  blocklist edit is a larger blast radius: a narrowly scoped, audited "unblock
  this exact release" action should only be added for entries Mintarr can prove
  it created, and should be treated as an explicit opt-in write exception to the
  read-only companion posture. A free-form blocklist editor is a later
  operator-power feature only if real use cases justify it; it must not let
  Mintarr silently rewrite Lidarr's blocklist without audit or provenance.

**Depends on:** Phase 1 (Connectors drive several of the new cards).

---

## Phase 3 — Observability and integration surface

**Goal:** Mintarr fits cleanly into existing self-hosting observability and notification stacks.

**Historical status at pause:** shipped — all scope delivered: structured JSON logging (`MINTARR_LOG_FORMAT=json`), Prometheus `/metrics` with a documented metric catalogue, Grafana dashboard templates under `docs/grafana/`, OpenAPI `/openapi.json` + Swagger UI at `/docs`, opt-in Apprise notifications (`MINTARR_NOTIFY_URLS`) on attention events, the backup/restore feature (read-only `GET /backup`, optional scheduled backup zips, and staged restore via `POST /restore` + crash-safe boot-time apply with System-section status/cancel controls), generic webhook-in (`POST /webhook/ingest`), and the MkDocs Material docs site. Enhancements such as Prometheus event counters/histograms and a richer event-metric surface were tracked as future work, not Phase 3 scope.

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

**Historical status at pause:** partial (TIDAL, LocalFolder, Soulseek, SAB completed-folder,
qBittorrent completed-folder, and the Mintarr-managed SAB backend lane shipped)

**Scope:**

- **F3.5a Soulseek/slskd completed-folder ingest** — implemented ([F3.5a completed-folder ingest design](../design/F3.5_SOULSEEK_COMPLETED_INGEST.md))
- **F3.5b Soulseek/slskd HTTP search and download** — implemented ([F3.5B Soulseek slskd trigger](../design/F3.5B_SOULSEEK_SLSKD_TRIGGER.md))
- **SAB/qBit completed-category ingest** — implemented as `sab_usenet` and `qbittorrent_torrent` completed-folder source connectors ([Phase 4 SAB/qBit completed ingest](../design/PHASE4_SAB_QBIT_COMPLETED_INGEST.md)). Scope is locked by [ADR-0012](../architecture/adr/0012-qc-import-gate-scope.md): Mintarr gates operator-routed completed folders; it does not manage download clients, and the universal "QC everything Lidarr imports" gate is a deferred future phase.
- **Mintarr-managed SAB/qBit music lane** — SAB backend lane shipped and
  live-validated for automatic Lidarr-triggered usenet music: Lidarr talks to
  Mintarr as the SAB-compatible download client, Mintarr submits to SAB as a
  backend transfer engine in a dedicated category, then copies through shared QC
  before Lidarr ManualImport ([Mintarr-managed SAB/qBit download-client
  lane](../design/MINTARR_MANAGED_SAB_QBIT_DOWNLOAD_CLIENT.md);
  [ADR-0014](../architecture/adr/0014-mintarr-managed-download-backend.md)).
  qBittorrent backend primitives exist, but the qBit end-to-end lane remains
  pending live wiring/dogfood. This is the robust path for "all music via
  Mintarr" without watching shared TV/movie completed roots.
- **CD-rip / private-tracker torrent evidence lane** — design pending; depends on Phase 1 connector model
- **YouTube fallback connector (Tubifarry-style)** — design pending; explicitly tagged as never-an-upgrade-source

**Depends on:** Phase 1 for new connectors to slot into the registry cleanly.

---

## Phase 5 — Quality refinement

**Goal:** Mintarr's verification and import decisions reflect the real-world ambiguity of music release data.

**Historical status at pause:** partial — F5.1 shipped; F5.3 shipped through the advisory tier (local CD-rip evidence + opt-in default-off scoring, plus a default-off network CTDB/AccurateRip lookup that surfaces advisory evidence); F5.4 library-evidence indexing and scan-tier architecture shipped through dogfood. Remaining Phase 5 work was focused on F5.2 source-aware thresholds, the heavier F5.3B checksum-recompute slice, adaptive scan concurrency, and broader dogfood before enabling measured-existing decisions by default.

**Scope:**

- **F5.1 Release-family matching** — **shipped**, architecture locked in [ADR-0013](../architecture/adr/0013-release-family-identity-policy.md). Mintarr-side mitigation of Lidarr's multi-album / edition / deluxe / remaster / anniversary matching weakness, built as a separate release-identity policy axis (two-axis audio/identity, where good audio never excuses the wrong album): Lidarr-first expected metadata with MusicBrainz/MBID as advisory evidence, read-only `mutagen` tag evidence, confidence + abstain (weak metadata → review, never block), dashboard visibility, and an opt-in **default-off** release-switch strategy with operator review-mode override (snapshot + audit + restore, same-album-only, never on `WRONG_ALBUM`/audio-`BLOCK`). The locked alternative-to-fork mechanism from [ADR-0007 §"Multi-album / release matching is not a fork problem"](../architecture/adr/0007-no-lidarr-fork.md). **Proposed follow-up:** [edition preference policy](../design/EDITION_PREFERENCE_POLICY.md) — operator-configurable manual/conservative/prefer-remaster/prefer-expanded/custom rules for valid editions inside the same release family.
- **F5.4 Library evidence index** — measure existing library files with the same sensor model and compare candidate vs *measured* existing quality, instead of trusting Lidarr's quality label ([F5.4 design](../design/F5.4_LIBRARY_EVIDENCE_INDEX.md); [F5.4 scan tiers](../design/F5.4_SCAN_TIERS.md); [F5.4 integrity classification](../design/F5.4_INTEGRITY_CLASSIFICATION.md); [ADR-0008 amendment](../architecture/adr/0008-strategic-positioning.md)). Read-only; Lidarr stays identity/ownership truth, Mintarr becomes quality truth. **Shipped:** on-demand per-album measurement into `library_evidence`, record-only import-time measurement, dashboard/detail visibility, the quality-vector comparison (validity → authenticity → lossless tier → completeness), explicit unknown-tier abstain, FLAC Detective spectral authenticity for existing files, and **opt-in default-off** decision use (`MINTARR_MEASURED_EXISTING`, plus `MINTARR_LIBRARY_SPECTRAL` and `MINTARR_REQUIRE_INTEGRITY` for stricter evidence use) that only acts on freshness-validated evidence and otherwise falls back to the Lidarr label. The full-library index is also shipped: operator-triggered metadata/integrity/spectral scan modes, metadata as the fast default pass, integrity as a separate `flac -t` tier, spectral as a separate default-off tier, default-off scheduled metadata scans that only queue when imports are idle, default-off `spectral_missing` background mode with item-ledger resume/skip and import-priority pause, and a Quality console with problem buckets, audio-tier distribution, lossy bitrate breakdown, cached aggregate summary, and Lidarr links. Dogfood classification fixes are shipped: stale FLAC MD5 is advisory `checksum_mismatch`, ID3-contaminated FLAC is advisory `nonstandard_flac_tags`, only real decode failures are `invalid`, and unknown integrity/authenticity is never shown as OK. **Remaining:** richer per-album drilldown/action groups, adaptive scan-concurrency mode (cgroup/affinity-aware hill-climbing instead of fixed metadata/integrity worker counts), and broader Lidarr end-to-end dogfood before changing any default decision flags.
- **F5.2 Source-aware verification thresholds** — per-source confidence weighting (e.g., Soulseek requires stricter spectral pass than TIDAL); not source-aware policy until evidence justifies it
- **F5.3 CD-rip evidence integration** — operational design in [F5.3 CD-rip evidence lane](../design/F5.3_CD_RIP_EVIDENCE_LANE.md). **Shipped:** read-only rip-log/cue parsing, an advisory `cd_rip_evidence` sensor surfaced in the dashboard, and opt-in **default-off** decision scoring (`MINTARR_CD_RIP_SCORING`) per §5.4 (AccurateRip-verified rip can lift a borderline review; log-backed mismatch routes to review; never overrides a hard gate or identity `WRONG_ALBUM`). **Also shipped (advisory):** the default-off network CTDB/AccurateRip verifier ([F5.3B](../design/F5.3B_CTDB_NETWORK_VERIFIER.md)) — TOC reconstruction, DB-validated AccurateRip/CDDB/CTDB disc-id + lookup conventions, cached/timeout-bounded lookup clients, a `ctdb` verifier connector, and an advisory `ctdb` sensor result. It is lookup-only and decision-neutral. **Pending:** sub-slice B — decode PCM and recompute AR/CTDB checksums so an online-*verified* rip can feed the opt-in decision (its own perf-budgeted slice).

**Depends on:** Phase 1 connector model; Phase 4 source coverage.

---

## Phase 6 — Output diversification

**Goal:** Mintarr is useful to operators who do not run Lidarr.

**Historical status at pause:** planned (post-V1.0)

**Scope:**

- Plex direct-import OutputConnector
- Jellyfin direct-import OutputConnector
- Filesystem-only OutputConnector (move verified files to a configured library path with no downstream manager)
- Multi-output routing (same import can target multiple outputs based on policy)

**Depends on:** Phase 1 connector model (OutputConnector is already a first-class concept).

---

## Phase 7 — Upstream coordination (opportunistic)

**Goal:** reduce Mintarr's Lidarr dependency from "always required" to "primary output among several".

**Historical status at pause:** opportunistic — intended to be pursued when one of the conditions in [ADR-0007 §Re-evaluation triggers](../architecture/adr/0007-no-lidarr-fork.md) was approached.

**Scope:**

- Pre-import webhook PR to Lidarr upstream (if upstream maintainers are receptive)
- Lidarr-native integration / plugin RFC: pass explicit `albumId`, release context, and operator intent into Mintarr at search/grab/import boundaries so Mintarr can avoid text-based album resolvers for album-holds and QC routing. This is the ideal long-term integration, but it must not block the v1-compatible Newznab/SAB path for unmodified Lidarr installs.
- Lidarr v4 client implementation alongside v3 client (capability-detection at boot)
- Coordination with Lidarr maintainers on Custom Format conventions for `[TIDAL]`, `[Local]`, `[Soulseek]` source tags

**Depends on:** Mintarr v1.0 release (adoption signal before approaching upstream).

---

## Phase 8 — Mintarr Audio Lab (additive sensors)

**Goal:** Mintarr ships its own audio analysis sensors that complement (not replace) the external verifier stack.

**Historical status at pause:** intended direction. Not scoped; not started. It was to be pursued only after Phase 4 source coverage and Phase 5 quality refinement produced clear gaps that in-house sensors would fill.

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
| Lidarr blocklist authority | Lidarr's blocklist stays source of truth. Mintarr records and explains blocklist operations it initiates, reflects Lidarr state where the API allows it, and only offers controlled, audited release-specific unblock/edit actions after provenance is known. |
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

> Last updated: 2026-07-26 (project pause; phase statuses frozen)
