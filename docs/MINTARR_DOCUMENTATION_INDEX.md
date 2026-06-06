# Mintarr — Documentation Index

> **Type:** Meta-document. Lists every planned Mintarr document, its status, owner and dependencies.
> **Version:** 0.22 — 2026-06-05
> **Status:** Public-repo tracker. Documentation set drafted. License AGPL-3.0-only. Language Python 3.12+ (ADR-0010). Cutover strategy: clean new repo, no inherited history ([MINTARR_CUTOVER_PLAYBOOK.md](architecture/MINTARR_CUTOVER_PLAYBOOK.md)). HTTP_API_v1 and SIDECAR_FORMAT_v2 runtime-validated and locked. F4.1-F4.5 foundation landed post-cutover; Phase 4 SAB/qBittorrent completed-folder ingest is implemented. v0.2.0 docs skeletons for troubleshooting, observability, and use cases are drafted. ADR-0011 locks the Phase 2 frontend approach. F2-F3.4 design docs and the quality stack roadmap are migrated and rebranded.
> **Audience:** Project maintainers (Eivind, Claude, Codex) coordinating doc work.

---

## 1. Why this index exists

Open-source projects fail their newcomers when documentation is "we'll write it eventually". Mintarr is being built deliberately for community contribution, which means docs are a deliverable, not a side effect. This index serves three roles:

1. **A map** — readers find the right document for their question.
2. **A backlog** — maintainers see what is missing and who owns it.
3. **A contract** — every spec document has a SemVer-versioned name so external adapter authors can pin against a stable surface.

The index itself is the first document of the Mintarr project. Everything else hangs off it.

---

## 2. Project foundation decisions

These decisions block document content and must remain stable while documentation is written.

| Decision | Value | Rationale |
|---|---|---|
| Project name | **Mintarr** | Arr-namespace ("mint condition" = audiophile-grade). Replaces "tidalhires". |
| License | **AGPL-3.0-only** (locked 2026-05-31, [ADR-0005](architecture/adr/0005-license.md)) | Combined work includes AGPL-3.0-only `tidal-dl-ng-For-DJ` fork; combined-work license is AGPL-3.0-only. AGPL §13 "Remote Network Interaction" applies to network-hosted deployments. |
| Repository | **`eivindsjursen-lab/mintarr`** (new public repo, to be created) | Clean community fork from the private `sjursen-mediastack` monorepo. |
| Docs tooling | **MkDocs Material** | Industry standard for technical docs, auto-deploys to GitHub Pages, built-in search. |
| Documentation language | **English only** | Maximises community reach; norsk reserved for internal working notes. |
| Instance model | **Single-instance per container** | Matches arr-stack. Multi-user is handled by reverse-proxy SSO (Authelia/Authentik) reading `Remote-User` header. |
| API versioning | **SemVer on adapter + connector + HTTP API surfaces** | Lets community-built adapters pin a stable contract. HTTP_API_v1 and SIDECAR_FORMAT_v2 were validated and locked on 2026-05-31; CONNECTOR_MANIFEST_v1 remains provisional until F4.1 lands. |
| Documentation pace | **No deadline** — quality first | Eivind's stated preference. |

ADRs (Architecture Decision Records) under `docs/architecture/adr/` capture the long-form reasoning behind each.

---

## 3. Migration from tidalhires

The Mintarr project starts as a rename of `tidalhires` (the music-QC subsystem inside `sjursen-mediastack`). Migration plan:

1. **Phase 0 (current):** documents are written under the existing `docs/` directory in `sjursen-mediastack`. Each Mintarr document is prefixed with `MINTARR_` or lives under a Mintarr subdirectory (`docs/strategy/`, `docs/architecture/`, etc) so the old `tidalhires`-era documents stay easy to spot.
2. **Phase 1 (cutover):** when foundation docs are complete and the maintainer creates `eivindsjursen-lab/mintarr`, sanitized source + selected Mintarr docs move to the new repo as a clean initial commit. The cutover uses an explicit assembly manifest captured in [MINTARR_CUTOVER_PLAYBOOK.md](architecture/MINTARR_CUTOVER_PLAYBOOK.md): source tree rename, env-var aliases, docs selected for publication, root files, CI inputs, link-check, mkdocs build and test commands. History-preserving alternatives were considered and explicitly rejected.
3. **Phase 2 (post-cutover):** the old `tidalhires/` directory in `sjursen-mediastack` becomes a stub README pointing at the new repo. The new repo becomes canonical.

Old `tidalhires/`-era documents stay in this index marked **legacy** and are either migrated, rewritten or archived during Phase 1.

---

## 4. Documentation layout (target shape)

```
mintarr/                                           ← root of the new public repo
├── README.md                                      ← landing page
├── LICENSE                                        ← AGPL-3.0-only (verbatim)
├── CHANGELOG.md                                   ← per-release notes (semver)
├── CODE_OF_CONDUCT.md                             ← Contributor Covenant 2.1
├── CONTRIBUTING.md                                ← PR process, coding standards
├── SECURITY.md                                    ← vuln-disclosure policy
├── mkdocs.yml                                     ← docs site config
├── docs/
│   ├── MINTARR_DOCUMENTATION_INDEX.md             ← (this file)
│   ├── strategy/
│   │   ├── VISION.md
│   │   ├── ROADMAP.md
│   │   └── GLOSSARY.md
│   ├── architecture/
│   │   ├── OVERVIEW.md
│   │   ├── PIPELINE.md
│   │   ├── DATA_MODEL.md
│   │   ├── SECURITY_MODEL.md
│   │   └── adr/
│   │       ├── 0001-rename-from-tidalhires.md
│   │       ├── 0002-single-instance-arr-pattern.md
│   │       ├── 0003-connector-vs-adapter.md
│   │       ├── 0004-api-versioning-semver.md
│   │       └── ...
│   ├── specs/
│   │   ├── ADAPTER_PROTOCOL_v1.md
│   │   ├── CONNECTOR_MANIFEST_v1.md
│   │   ├── SIDECAR_FORMAT_v2.md
│   │   ├── HTTP_API_v1.md
│   │   └── LIDARR_INTEGRATION.md
│   ├── operations/
│   │   ├── INSTALL.md
│   │   ├── CONFIGURATION.md
│   │   ├── UPGRADE_GUIDE.md
│   │   ├── TROUBLESHOOTING.md
│   │   ├── BACKUP_RESTORE.md
│   │   └── OBSERVABILITY.md
│   ├── development/
│   │   ├── DEVELOPMENT.md
│   │   ├── TESTING.md
│   │   ├── STYLE_GUIDE.md
│   │   ├── COMMIT_CONVENTION.md
│   │   ├── REVIEW_CHECKLIST.md
│   │   ├── AGENT_HANDOVER.md
│   │   └── ADAPTER_TUTORIAL.md
│   ├── design/                                    ← per-feature design docs (F-numbered)
│   │   └── *.md
│   └── community/
│       ├── USE_CASES.md
│       ├── COMPARISON.md
│       └── FAQ.md
└── .github/
    ├── ISSUE_TEMPLATE/
    │   ├── bug_report.yml
    │   ├── feature_request.yml
    │   └── connector_request.yml
    ├── PULL_REQUEST_TEMPLATE.md
    └── workflows/
        ├── ci.yml
        ├── build.yml
        └── docs.yml
```

---

## 5. Documentation principles

Hard rules every Mintarr document must follow:

1. **Stay in lane.** `CONTRIBUTING.md` does not explain architecture. `ARCHITECTURE.md` does not explain PR process. If a question fits two documents, link, don't duplicate.
2. **SemVer the specs.** `ADAPTER_PROTOCOL_v1.md`, `CONNECTOR_MANIFEST_v1.md`, `SIDECAR_FORMAT_v2.md` — the filename version is the contract version. Breaking changes get a new file (`_v2.md`); the old file is kept for community adapters still on the older contract.
3. **Show invariants, not just behaviour.** Every spec must list what it guarantees and what it forbids. Lets implementers reason about safety.
4. **Locked decisions get an ADR.** Every "we considered X and Y, chose X because Z" gets an ADR file under `docs/architecture/adr/`. Numbered, append-only, never edited after lock (superseded with a new ADR instead).
5. **No secrets in docs.** Examples use placeholder values (`<your-api-key>`, `example.com`). Real credentials live in env or Docker secrets.
6. **Markdown only, ASCII-friendly.** Diagrams as Mermaid or PlantUML in fenced code blocks (MkDocs Material renders both). Avoid binary images where text works.
7. **Updated-at footer on living docs.** Strategy and operational docs that change over time end with `> Last updated: YYYY-MM-DD`. Spec versions are immutable once locked.
8. **Private runbooks stay private.** Handover docs, dogfood logs, incident reports, local compose files, host paths, tracker-specific policy and personal troubleshooting notes are source material for rewritten public docs, not public docs themselves.

---

## 6. Document tracker

Legend:
- **Status:** `planned` / `drafted` (first version exists, may need review) / `locked` (reviewed and stable) / `outdated` (needs rewrite) / `legacy` (from tidalhires era)
- **Owner:** who's responsible for first draft. After lock, all maintainers may patch.
- **Prio:** `P0` blocks Phase 0 (foundation). `P1` blocks first community release. `P2` is post-V1.0.

### 6.1 Project root

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `README.md` | drafted (in `mintarr/`) | Claude | P0 | Mintarr-focused landing page; references ADR-0008 verbatim. Staged in `mintarr/` subdirectory pending cutover. |
| `LICENSE` | locked | Claude/Eivind | P0 | AGPL-3.0-only verbatim. ADR-0005 locked 2026-05-31. |
| `CHANGELOG.md` | drafted (in `mintarr/`) | Claude | P1 | Keep-a-Changelog format. Foundation phase recorded; pre-rename tidalhires history acknowledged. |
| `CODE_OF_CONDUCT.md` | drafted (in `mintarr/`) | Claude | P1 | Contributor Covenant 2.1 verbatim. |
| `CONTRIBUTING.md` | drafted (in `mintarr/`) | Claude | P0 | PR process, boundary-test reference, adapter/docs/test expectations. |
| `SECURITY.md` | drafted (in `mintarr/`) | Claude | P0 | Vuln-disclosure via GitHub PVR + email fallback, threat-model summary, 90-day coordinated disclosure default. |
| `mkdocs.yml` | drafted (in `mintarr/`) | Claude | P1 | Material theme, Mermaid plugin, navigation mirrors index §4. |
| `docker-compose.example.yml` | drafted (in `mintarr/`) | Claude | P0 | Placeholder values only; documented mount points; reverse-proxy SSO note. |

### 6.2 Strategy (`docs/strategy/`)

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `VISION.md` | drafted | Claude | P0 | References ADR-0008 verbatim. Defines target audiences, locked owned/not-owned scope. |
| `ROADMAP.md` | drafted | Claude | P0 | Phases 0–7 with status, scope, dependencies. F5.1 release-family matching included. |
| `GLOSSARY.md` | drafted | Claude | P0 | ~80 terms grouped by domain (core abstractions, pipeline, verification, sources, quality, operator surface, governance, arr-stack, migration). |

### 6.3 Architecture (`docs/architecture/`)

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `OVERVIEW.md` | drafted | Claude | P0 | Component diagram (ASCII), 4-phase pipeline summary, two example request flows (TIDAL via Lidarr search, LocalFolder via /local/ingest), coupling-boundary table. |
| `PIPELINE.md` | drafted | Claude | P1 | All 4 phases with per-phase invariants, cancellation, failure modes, coupling rules. |
| `DATA_MODEL.md` | drafted | Claude | P1 | Three persistence layers, full state_db schema, migration policy, decisions.jsonl, filesystem layout, backup pointer. |
| `SECURITY_MODEL.md` | drafted | Claude | P1 | Trust boundaries, secrets handling, input validation, subprocess, network surface, source integrity, 10 invariants, threat scenarios. |
| `CUTOVER_MANIFEST.md` | drafted | Codex | P0 | Required assembly/checklist before creating the public repo; covers source layout, allowed docs, contract validation, link/docs build, security scrub and CI gates. |
| `LICENSE_COMPATIBILITY.md` | planned | Codex | P0 | Dependency/license table + policy for AGPL TIDAL downloader fork and container redistribution. |
| `adr/0001-rename-from-tidalhires.md` | drafted | Claude | P0 | Records the rename decision; alternative names considered; arr-namespace rationale. |
| `adr/0002-single-instance-arr-pattern.md` | drafted | Claude | P0 | Records the single-instance + API-key/form-login/proxy-SSO decision; pattern-fit with arr-stack. |
| `adr/0003-connector-vs-adapter.md` | drafted | Claude (TBD: Codex re-author?) | P0 | Records the connector/adapter split; rationale per Codex's architecture doc. |
| `adr/0004-api-versioning-semver.md` | drafted | Claude | P1 | Records the SemVer-on-Protocols decision; `_vN.md` filename convention. |
| `adr/0005-license.md` | locked | Claude/Eivind | P0 | AGPL-3.0-only chosen 2026-05-31. Three pending alternatives preserved as historical context within the ADR. |
| `adr/0006-docs-tooling-mkdocs-material.md` | drafted | Claude | P0 | Records MkDocs Material choice; alternatives considered. |
| `adr/0007-no-lidarr-fork.md` | locked | Claude/Eivind | P0 | Locks the no-fork decision. Re-evaluation triggers explicitly enumerated; any fork-shaped proposal closes against this ADR. |
| `adr/0008-strategic-positioning.md` | locked | Claude/Eivind | P0 | Locks Mintarr's positioning: "the QC and import orchestration layer Lidarr lacks". Includes boundary test for feature proposals. Referenced verbatim from `README.md`, `VISION.md`, `COMPARISON.md`. |
| `adr/0009-runtime-hardening-conventions.md` | locked | Codex/Eivind | P0 | Locks cutover hardening conventions: authenticated/redacted fallback routes, `MINTARR_*` aliases before legacy removal, and server-injected dashboard config. |
| `adr/0010-python-implementation-language.md` | locked | Claude/Eivind | P0 | Locks Python 3.12+ as implementation language. Prevents future rewrite-to-C#-or-Rust proposals without explicit successor ADR. |
| `adr/0011-frontend-framework.md` | locked | Claude/Codex | P2 | Locks Phase 2 dashboard on server-rendered Flask with HTMX + Alpine.js, no Node toolchain, and no SPA framework. |
| `adr/0012-qc-import-gate-scope.md` | locked | Claude/Eivind | P1 | Scopes Mintarr as the QC import gate for Mintarr-routed sources only (own lanes + operator-routed completed folders); Lidarr-client discovery is onboarding, universal gate deferred to a future ADR. |
| `adr/0013-release-family-identity-policy.md` | locked | Claude/Codex/Eivind | P2 | Locks the F5.1 release-family identity-policy architecture: Lidarr-first metadata, MusicBrainz advisory evidence, separate audio/identity axes, confidence/abstain behavior, and default-off audited release switching. |

### 6.4 Specs (`docs/specs/`)

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `ADAPTER_PROTOCOL_v1.md` | drafted | Claude | P1 | SemVer-versioned contract for SourceAdapter authors. Complete API + dataclass spec + worked example. |
| `CONNECTOR_MANIFEST_v1.md` | locked | Claude/Codex | P1 | Runtime-backed by F4.1 static connector registry and `/dashboard/v1/connectors`. |
| `SIDECAR_FORMAT_v2.md` | locked | Claude/Codex | P1 | Runtime-validated 2026-05-31 against imported, blocked and review sidecars. |
| `HTTP_API_v1.md` | locked | Claude/Codex | P1 | Runtime-validated 2026-05-31 against Flask route inventory (33 routes). |
| `LIDARR_INTEGRATION.md` | drafted | Claude | P1 | Supported Lidarr versions, endpoint catalogue, multi-version client design, version-specific quirks (3.1.x reference). |

### 6.5 Operations (`docs/operations/`)

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `INSTALL.md` | drafted | Claude | P1 | Docker Compose quickstart, Lidarr integration, source adapter setup, Custom Format guidance, end-to-end verification. |
| `CONFIGURATION.md` | drafted | Claude | P1 | All env vars catalogued with defaults; volume mount reference; networking; CF scoring recommendations. |
| `docker-compose.example.yml` | drafted (in `mintarr/`) | Claude | P0 | Portable example compose. Local host paths and private service topology must not be published as defaults. |
| `UPGRADE_GUIDE.md` | drafted | Claude | P1 | tidalhires→mintarr migration; SemVer migration policy; rollback; Lidarr coordination. |
| `TROUBLESHOOTING.md` | drafted | Codex | P2 | Common issues + fixes. Grows from issue tracker, dogfood runs, and operator reports. |
| `BACKUP_RESTORE.md` | drafted | Claude | P1 | Live + cold backup procedures; selective restore; disaster recovery; verification routine. |
| `OBSERVABILITY.md` | drafted | Codex | P2 | Current logs/dashboard/sidecar surfaces plus planned structured logging, Prometheus metrics catalog, and Grafana templates. |

### 6.6 Development (`docs/development/`)

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `DEVELOPMENT.md` | drafted | Claude | P0 | Docker-only setup, dev loop, per-subsystem guidance (adapters, pipeline, V2, state_db, dashboard). |
| `TESTING.md` | drafted | Claude | P1 | Test philosophy, fast/isolated/deterministic principles, fixtures, patterns (adapter, pipeline phase, Flask endpoint, mocked Lidarr), debugging. |
| `STYLE_GUIDE.md` | drafted | Claude | P1 | ruff + mypy + Python 3.12. Naming, imports, type hints, docstrings, error handling, logging, subprocess, Flask conventions. |
| `COMMIT_CONVENTION.md` | drafted | Claude | P1 | Conventional Commits format, types, scopes, SemVer impact mapping, AI co-author attribution. |
| `REVIEW_CHECKLIST.md` | drafted | Claude | P1 | 16 reviewer-concern sections: scope, tests, docs, boundaries, security, error handling, concurrency, performance, style, commits, compatibility. |
| `AGENT_HANDOVER.md` | drafted (Mintarr v2.0) | Claude | P0 | Rewritten for Mintarr. Hard invariants list, anti-patterns observed, decisions-not-to-relitigate table, agent-coordination conventions. |
| `ADAPTER_TUTORIAL.md` | drafted | Claude | P1 | End-to-end FTP adapter walkthrough: plan, skeleton, is_enabled, search, download_raw, registration, manifest, tests, common mistakes. |

### 6.7 Design docs (`docs/design/`)

Per-feature design docs. Numbered F-series (F3.1, F3.4, F4.1, etc). Each one follows the pattern Codex established for F3.x: draft → review → lock → implementation record.

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `F2_WORKER_QUEUE_DESIGN.md` | implemented | Claude/Codex | P1 | Migrated and rebranded public design record for the SQLite worker queue. |
| `F3_SOURCE_ADAPTERS_DESIGN.md` | implemented | Claude/Codex | P1 | Migrated and rebranded public design record for the SourceAdapter boundary. |
| `F3.2_F3.3_NEWZNAB_ROUTING_DESIGN.md` | implemented | Claude/Codex | P1 | Migrated and rebranded public design record for Newznab aggregation and source-aware SAB routing. |
| `F3.4_LOCAL_FOLDER_DESIGN.md` | implemented | Claude/Codex | P1 | Migrated and rebranded public design record for LocalFolder ingest. |
| `QUALITY_STACK_ROADMAP.md` | drafted | Claude/Codex | P1 | Migrated and rebranded public roadmap for the pre-import quality gate and future sensor lanes. |
| `CONNECTOR_PLUGIN_ARCHITECTURE.md` | drafted | Codex | P1 | Becomes basis for F4.1-F4.5 design docs. |
| `F4.1_STATIC_CONNECTOR_REGISTRY.md` | implemented | Claude/Codex | P0 | Static connector registry, built-in manifests, GET /dashboard/v1/connectors, and registry invariants landed. |
| `F4.2_INTEGRATIONS_DASHBOARD.md` | implemented | Codex | P2 | UI tab for connector status, grouped source/verifier/output inventory, no config mutation. |
| `F4.3_CONNECTOR_CONFIG_DRY_RUN.md` | implemented | Codex | P2 | Connector config persistence, dry-run validation, mode controls, and source runtime gates. |
| `F4.4_CONNECTOR_INSTALL_GUIDANCE.md` | implemented | Codex | P2 | Secret-safe install guidance derived from connector manifests and rendered in the Integrations dashboard. |
| `F4.5_OPTIONAL_VERIFIER_CONNECTORS.md` | implemented | Codex | P2 | First optional metadata-identity verifier represented in connector and sensor registries, disabled by default. |
| `PHASE4_SAB_QBIT_COMPLETED_INGEST.md` | implemented | Codex | P1 | SABnzbd and qBittorrent completed-folder source connectors under ADR-0012 scoped-gate rules. |
| `F3.5_SOULSEEK_COMPLETED_INGEST.md` | implemented | Codex | P1 | Soulseek completed-folder ingest through connector registry, `/soulseek/ingest`, copy-only adapter, completed-folder safety checks. |
| `F3.5B_SOULSEEK_SLSKD_TRIGGER.md` | implemented | Codex | P1 | slskd-backed Soulseek search/download trigger through existing Newznab/SAB flow. |
| `F5.1_RELEASE_FAMILY_MATCHING.md` | planned | Claude | P2 | Mintarr-side mitigation of Lidarr's multi-album / edition matching weakness. Locked-in feature from ADR-0007 §"Multi-album / release matching is not a fork problem". Includes: release-family scoring, track-count + track-title similarity, edition-aware import policy, dashboard explanation when Lidarr rejects, optional manual override with audit. Estimate 15-25h. |

### 6.8 Community (`docs/community/`)

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `USE_CASES.md` | drafted | Codex | P2 | Who uses Mintarr, how. Grows from real deployments. |
| `COMPARISON.md` | drafted | Claude | P2 | Side-by-side vs Lidarr alone, Beets, Picard, slskd/Soularr, custom scripts. Includes "when Mintarr is the wrong choice". |
| `FAQ.md` | drafted (stub) | Claude | P2 | Question stub organised by topic. Grows from issues/Discussions. |

### 6.9 GitHub infra (`.github/`)

| Path | Status | Owner | Prio | Notes |
|---|---|---|---|---|
| `ISSUE_TEMPLATE/bug_report.yml` | drafted (in `mintarr/`) | Claude | P1 | Structured YAML form: Mintarr/Lidarr versions, deployment shape, source adapter, reproduction, logs, sidecar JSON. |
| `ISSUE_TEMPLATE/feature_request.yml` | drafted (in `mintarr/`) | Claude | P1 | Problem, proposal, alternatives, boundary-test placement, importance. |
| `ISSUE_TEMPLATE/connector_request.yml` | drafted (in `mintarr/`) | Claude | P2 | Connector kind, upstream URL+license, API surface, provenance, implementation plan. |
| `PULL_REQUEST_TEMPLATE.md` | drafted (in `mintarr/`) | Claude | P1 | Summary, what changed, why, test plan, boundary test, breaking changes, security checklist. |
| `workflows/ci.yml` | drafted (in `mintarr/`) | Claude | P1 | pytest in container, ruff check + format, mypy, conventional-commits linting. |
| `workflows/build.yml` | drafted (in `mintarr/`) | Claude | P1 | Multi-arch container build, push to GHCR on tags + main, SemVer tag extraction. |
| `workflows/docs.yml` | drafted (in `mintarr/`) | Claude | P1 | MkDocs Material build with `--strict`, deploy to GitHub Pages. |
| `.commitlintrc.yaml` | drafted (in `mintarr/`) | Claude | P1 | Conventional-Commits config consumed by ci.yml. |

---

## 7. Foundation pass — what must exist before Phase 0 coding starts

The **P0 set** (must exist before we begin the rename + foundation code work):

| # | Document | Estimate | Blocker for |
|---|---|---|---|
| 1 | `docs/MINTARR_DOCUMENTATION_INDEX.md` | done (this file) | All other docs |
| 2 | `docs/architecture/LICENSE_COMPATIBILITY.md` | 1-2t | Final license decision |
| 4 | `docs/architecture/adr/0005-license-gpl-3-0.md` | 30-60 min after audit | `LICENSE` |
| 5 | `docs/strategy/VISION.md` | 2t | Scope decisions in any other doc |
| 6 | `docs/strategy/GLOSSARY.md` | 1-2t | Consistent terminology in everything else |
| 7 | `docs/architecture/adr/0003-connector-vs-adapter.md` | 1t | Adapter/connector terminology in specs |
| 8 | `docs/architecture/OVERVIEW.md` | 3-4t | Community PR onboarding |
| 9 | `docs/strategy/ROADMAP.md` | 2t | Phase planning |
| 10 | `docs/architecture/adr/0001-rename-from-tidalhires.md` | 1t | Justifies the rename work |
| 11 | `docs/architecture/adr/0002-single-instance-arr-pattern.md` | 1t | Prevents future "let's go multi-tenant" rewrites |
| 12 | `docs/architecture/adr/0006-docs-tooling-mkdocs-material.md` | 30 min | Records tooling decision |
| 13 | `LICENSE` | 30 min after license ADR | Legal pre-req for public publication |
| 14 | `README.md` (new, Mintarr-focused) | 2-3t | First page community sees |
| 15 | `CONTRIBUTING.md` | 2-3t | PR process gatekeeper |
| 16 | `SECURITY.md` | 1t | Vuln-disclosure path |
| 17 | `docs/development/DEVELOPMENT.md` | 2-3t | New-contributor onboarding |
| 18 | `docs/development/AGENT_HANDOVER.md` (rewritten) | 2-3t | Claude/Codex working memory |
| 19 | `docker-compose.example.yml` | 1-2t | Portable install path |
| 20 | `docs/design/F4.1_STATIC_CONNECTOR_REGISTRY.md` | 2-3t | Connector implementation foundation |
| ✓ | `docs/architecture/adr/0007-no-lidarr-fork.md` | done 2026-05-26 | Locked fork rejection with re-evaluation triggers |
| ✓ | `docs/architecture/adr/0008-strategic-positioning.md` | done 2026-05-26 | Locked positioning + boundary test |

**Total estimate: ~28-40 hours.** Spread over a couple of weeks at no-stress pace.

After this set is locked, Phase 0 coding (rename + foundation refactor) can begin without re-deriving context from scratch.

---

## 8. Coordination

Owner column is **first-draft owner**, not lifetime maintainer. After lock, any maintainer (Claude, Codex, Eivind, community) may patch with PR review.

When a Mintarr document is created or status changes, the editor updates this index in the same commit. Stale index entries are a documentation bug — flag with `[outdated]` comment until rewritten.

When Codex and Claude work in parallel, they coordinate via this index — the **Owner** column claims a draft. If two agents need to touch the same document, the second waits or rebases.

---

## 9. Open questions

1. **MkDocs Material site domain:** start with GitHub Pages default (`<org>.github.io/mintarr` or `mintarr.github.io` if a dedicated org exists). Defer custom domain until there is public adoption.
2. **Documentation versioning:** start with `latest` only. Add `mike` versioned docs at first stable release, not during foundation work.
3. **Translation infrastructure:** English-only. Do not configure Crowdin/Weblate now; add an ADR later if community demand appears.

---

## Changelog

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-05-26 | Initial Mintarr documentation index. Lists ~30 planned documents, foundation decisions, P0 set for Phase 0 coding start. |
| 0.2 | 2026-05-26 | Codex review updates: public cutover blocked until publication scrub + license audit, connector-vs-adapter moved to P0, F4.1 registry moved earlier, open questions answered. |
| 0.3 | 2026-05-26 | Strategic positioning locked via ADR-0007 (no Lidarr fork) and ADR-0008 (Mintarr is the QC and import orchestration layer Lidarr lacks). F5.1 release-family matching added as planned design doc — Mintarr-side mitigation for Lidarr's multi-album weakness, derived from ADR-0007 §"Multi-album / release matching is not a fork problem". |
| 0.4 | 2026-05-26 | P0 documentation batch drafted: VISION, ROADMAP, GLOSSARY, OVERVIEW, ADRs 0001–0006 (0005 pending Eivind's license choice among three alternatives), README, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, CHANGELOG, mkdocs.yml, docker-compose.example.yml, DEVELOPMENT.md, AGENT_HANDOVER.md (Mintarr v2.0 rewrite), F4.1_STATIC_CONNECTOR_REGISTRY.md. Cutover blockers reduced to (a) license decision and (b) publication-audit scrub list. |
| 0.5 | 2026-05-26 | Complete P1 + P2 documentation set drafted: specs (ADAPTER_PROTOCOL_v1, CONNECTOR_MANIFEST_v1, SIDECAR_FORMAT_v2, HTTP_API_v1, LIDARR_INTEGRATION), architecture deep-dives (PIPELINE, DATA_MODEL, SECURITY_MODEL), operations (INSTALL, CONFIGURATION, UPGRADE_GUIDE, BACKUP_RESTORE), development (TESTING, STYLE_GUIDE, COMMIT_CONVENTION, REVIEW_CHECKLIST, ADAPTER_TUTORIAL), community (COMPARISON, FAQ stub), GitHub infra (3 issue templates, PR template, 3 workflows, .commitlintrc.yaml). All await Codex holistic review. Cutover blockers unchanged: ADR-0005 license + publication-audit scrub. |
| 0.6 | 2026-05-31 | Codex hardening review (commit 07ecfa4 + 1b60ac3). HTTP_API and SIDECAR_FORMAT downgraded to draft after contract drift found vs runtime; CONNECTOR_MANIFEST marked provisional pending F4.1. ADAPTER_PROTOCOL_v1 validated against runtime and re-locked. CUTOVER_MANIFEST.md added. Runtime hardening: catch-all auth + redaction, MINTARR_* env aliases, hardcoded LAN IP removed. ADR-0009 records the hardening pattern. Scripts added: check_markdown_links, inventory_flask_routes, validate_sidecar_format. 308 tests pass. |
| 0.7 | 2026-05-31 | **ADR-0005 locked: AGPL-3.0-only.** `LICENSE` file added (verbatim AGPL-3.0). README, CONTRIBUTING, mkdocs.yml footer, and CHANGELOG all reflect AGPL-3.0. AGPL §13 operator obligations called out where relevant. Remaining cutover blockers reduced to (a) HTTP_API + SIDECAR_FORMAT fixture validation, (b) publication-audit P0 scrub, (c) F4.1 implementation for CONNECTOR_MANIFEST lock. |
| 0.8 | 2026-05-31 | **Cutover strategy locked: clean new repo, no inherited history.** `docs/architecture/MINTARR_CUTOVER_PLAYBOOK.md` added — 10-phase operational playbook with exact commands for Eivind to follow. F4.1 implementation explicitly deferred to post-cutover (better sequence: clean cutover → spec validation → F4.1 → feature work). Publication-audit P0 scrub now happens implicitly via Phase A selective copy rather than as a separate gate. |
| 0.9 | 2026-05-31 | **HTTP_API_v1 and SIDECAR_FORMAT_v2 locked after runtime validation.** Route inventory covered 33 Flask routes; HTTP_API now documents legacy verification and infrastructure endpoints. Sidecar validation covered imported, blocked and review sidecars; SIDECAR_FORMAT now documents deployed optional legacy fields, sensor metadata, file metadata and `severity=info`. Cutover blocker list reduced to Eivind executing the playbook. |
| 0.10 | 2026-05-31 | **Strategic-direction batch: ADR-0010 + Phase 8 + VISION-orchestration-clarification.** ADR-0010 locks Python 3.12+ as implementation language (preempts future "rewrite in C# for arr-stack" or "translate to Rust" proposals). ROADMAP adds Phase 8 "Mintarr Audio Lab" — additive in-house sensors complementing (not replacing) external verifiers. VISION clarifies that Mintarr orchestrates verifier tools in v1; in-house sensors are future-direction, not current state. No cutover-blocker impact; these align expectations for post-cutover work. |
| 0.11 | 2026-05-31 | **Codex strategic-direction review.** ADR-0010 wording tightened after codebase validation: application runtime is ~7.4k Python LOC, public contract/cutover scripts are Python while private Windows incident helpers may remain PowerShell, Phase 8 reference fixed, and Python ecosystem examples corrected. ADR table duplicate removed. No structural disagreement with ADR-0010, Phase 8, or VISION orchestration wording. |
| 0.12 | 2026-06-01 | **Post-cutover implementation tracker update.** F4.1-F4.3 are reflected as landed public-repo work. F3.5a Soulseek completed-folder ingest is tracked as implemented with runtime docs and API spec updates. |
| 0.13 | 2026-06-03 | **v0.2.0 docs skeletons drafted.** Added TROUBLESHOOTING.md, OBSERVABILITY.md, and USE_CASES.md; MkDocs nav and INSTALL next-step links now resolve. |
| 0.14 | 2026-06-03 | **ADR-0011 frontend decision locked.** Phase 2 dashboard will use server-rendered Flask with HTMX + Alpine.js, vendored static assets, and no Node toolchain or SPA framework. TESTING.md updated for current ruff/mypy and targeted Playwright expectations. |
| 0.15 | 2026-06-03 | **F-series design migration batch 1.** Migrated and rebranded F2 worker queue, F3 source adapters, F3.2/F3.3 Newznab routing, and F3.4 LocalFolder from legacy private docs into public `docs/design/`. |
| 0.16 | 2026-06-03 | **Quality roadmap migration batch 2.** Migrated and rebranded `QUALITY_STACK_ROADMAP.md` from the legacy private docs into public `docs/design/`. |
| 0.17 | 2026-06-03 | **F4.4 connector install guidance implemented.** Added secret-safe install guidance payloads for connectors and dashboard/operator documentation for missing service/env/mount setup. |
| 0.18 | 2026-06-03 | **ADR-0012 QC import-gate scope locked.** Mintarr is the QC import gate for Mintarr-routed sources only (its own lanes + operator-routed completed folders); reading Lidarr's download-client config is onboarding/discovery, not a coverage guarantee, and the universal "QC everything Lidarr imports" gate is a deferred future phase. |
| 0.19 | 2026-06-04 | **ADR-0013 release-family identity policy drafted.** F5.1 is framed as a separate identity-policy axis beside audio QC: Lidarr-first expected metadata, MusicBrainz/advisory identity evidence, confidence/abstain behavior, and default-off audited release switching. |
| 0.20 | 2026-06-05 | **F5.1 + Phase 2 + Phase 3 core shipped.** ADR-0013 locked; F5.1 release-family identity implemented end to end (scoring, tag evidence, two-axis policy, dashboard visibility, opt-in default-off release switch + operator override). Phase 2 operator UI complete (sidebar shell, Alpine + HTMX, theme/density, live Queue/History/System, audit feed + CSV, search). Phase 3 observability core: structured JSON logging, Prometheus `/metrics`, OpenAPI `/openapi.json` + Swagger UI `/docs`, opt-in Apprise notifications. Docs updated: OBSERVABILITY, CONFIGURATION, FRONTEND_ASSETS, HTTP_API_v1, ROADMAP. |
| 0.22 | 2026-06-05 | **F5.3 CD-rip evidence lane (local) shipped.** Design ([F5.3 CD-rip evidence lane](design/F5.3_CD_RIP_EVIDENCE_LANE.md)) plus four slices landed: read-only rip-log/cue parser, advisory `cd_rip_evidence` sensor (`source_specific_proof`) surfaced in the dashboard, and opt-in default-off decision scoring (`MINTARR_CD_RIP_SCORING`) per quality-stack §5.4. Conservative + decision-neutral until opted in; never overrides a hard gate or identity `WRONG_ALBUM`. The network CTDB/AccurateRip online cross-check is deferred to its own design beat. Docs updated: SIDECAR_FORMAT_v2, CONFIGURATION, ROADMAP. |
| 0.21 | 2026-06-05 | **Phase 3 shipped (all scope delivered).** Completed the observability + integration surface: Grafana dashboard templates under `docs/grafana/` with a documented metric catalogue, generic webhook-in (`POST /webhook/ingest`), and the full backup/restore feature — read-only export (`GET /backup`), optional scheduled backup zips, and staged restore (validator → `POST /restore` staging → crash-safe boot-time apply with safety snapshot + fail-closed recovery → System-section status/cancel controls), per [Phase 3 restore endpoint design](design/PHASE3_RESTORE_ENDPOINT_DESIGN.md). Docs updated: BACKUP_RESTORE, CONFIGURATION, HTTP_API_v1, OBSERVABILITY, ROADMAP. Remaining (Prometheus event counters/histograms, richer event metrics) is future enhancement, not Phase 3 scope. |

> Last updated: 2026-06-04
