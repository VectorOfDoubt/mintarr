# ADR-0012: QC import-gate scope — Mintarr-routed sources first

**Status:** Accepted — locked 2026-06-03
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0003 Connector vs Adapter](0003-connector-vs-adapter.md), [ADR-0007 No Lidarr fork](0007-no-lidarr-fork.md), [ADR-0008 Strategic positioning](0008-strategic-positioning.md), [CONNECTOR_PLUGIN_ARCHITECTURE](../../design/CONNECTOR_PLUGIN_ARCHITECTURE.md), [ROADMAP Phase 4](../../strategy/ROADMAP.md)

---

## Context

Mintarr's purpose is to be the pre-import quality-control gate Lidarr lacks ([ADR-0008](0008-strategic-positioning.md)). Today it gates the sources it fully controls: TIDAL and Soulseek (where Mintarr is the indexer + SAB-compatible download client to Lidarr) and LocalFolder. The source-connector model already carries the invariant **"no source bypasses shared QC"** ([CONNECTOR_PLUGIN_ARCHITECTURE §6.1](../../design/CONNECTOR_PLUGIN_ARCHITECTURE.md)) — hard for the Mintarr-routed source jobs that enter the shared pipeline, but not an external-system guarantee: nothing in Mintarr can prevent Lidarr from importing content Lidarr grabbed on its own. The roadmap already lists `sab_usenet` and `qbittorrent_torrent` as future *completed-folder / category ingest* source connectors ([ROADMAP Phase 4](../../strategy/ROADMAP.md)).

A reasonable and tempting reading of "QC filter in front of Lidarr" is: **everything Lidarr imports should be quality-controlled**, including releases Lidarr itself grabs from other indexers (Prowlarr → qBittorrent / SABnzbd) and imports on its own. That "universal gate" is strategically attractive but technically far more entangled, because Lidarr owns the grab→import lifecycle for that content: making Mintarr the gate there forces Mintarr to either become the download client Lidarr talks to, or to intercept Lidarr's own grab/import cycle — pulling in queue reconciliation (the same dangling-queue-row class of problem PR #32 fixed for Mintarr's own lane), torrent seeding semantics, category mapping, and a "who owns import status?" split-brain.

A related onboarding idea surfaced: Mintarr could read Lidarr's configured download clients via the Lidarr API to help operators set up which completed folders Mintarr watches. That is useful, but it must not be confused with a guarantee that everything Lidarr does is gated.

This ADR scopes **how far the QC gate reaches now**, so the boundary is a deliberate decision rather than connector-by-connector drift.

## Decision

**Mintarr is the QC and import gate for Mintarr-routed sources only.** A source is "Mintarr-routed" when Mintarr owns its import path end-to-end:

1. **Mintarr-sourced lanes** — Mintarr is the indexer and/or downloader (TIDAL, Soulseek/slskd, LocalFolder). Mintarr owns fetch → QC → import.
2. **Operator-routed completed folders** — qBittorrent/SABnzbd (and similar) completed-download *categories or folders that the operator explicitly points at Mintarr*. Mintarr watches the completed output read-only, runs shared QC, and owns the import step (Lidarr ManualImport) for that lane.

For these, the source-connector invariant holds at the positioning level: **no Mintarr-routed source bypasses shared QC.**

Three things are explicitly locked alongside:

- **Lidarr download-client discovery via API is onboarding, not a guarantee.** Mintarr may read Lidarr's download-client and remote-path-mapping APIs (`GET /api/v1/downloadclient`, `GET /api/v1/remotepathmapping`) to *infer or prefill candidate mappings* the operator then confirms. This is discovery/mapping assistance — and only inferential: category and on-disk path are typically inside the client's generic `fields[]`, and remote-path-mappings translate client paths to Lidarr-local paths without by themselves guaranteeing a complete Mintarr-watchable folder. It does **not** mean Mintarr manages those clients, and it does **not** imply that everything Lidarr imports is QC'd.

- **The universal gate is a future phase, not now.** Guaranteeing QC for *everything Lidarr imports* — including content Lidarr grabs from other indexers and imports itself — is deferred to a successor ADR, to be revisited only after the scoped model is stable in operation. Mintarr does not silently intercept Lidarr's own grab/import lifecycle in this scope.

- **The operator contract is explicit.** A source is QC-gated **only when Mintarr owns its import step.** For client completed-folders, that means the operator routes the category to Mintarr and does not also have Lidarr import the same folder directly. Where Lidarr imports content itself, Lidarr owns it and it is not gated. There is no split import-status ownership.

## Rationale

### It satisfies the positioning without crossing the boundary

[ADR-0008](0008-strategic-positioning.md)'s boundary test puts "source coverage" and "import safety" *in scope* and "download-client ecosystem configuration" *out of scope* — Mintarr "exposes itself *as* a SAB-compatible download client to Lidarr but does not configure others." The scoped gate is exactly that line: Mintarr covers more sources and gates their imports, via read-only completed-folder ingest, without managing or proxying anyone's download clients. The completed-folder mechanism also keeps torrent **seeding intact** (Mintarr reads, it does not consume the client's data).

### It honours who owns the import lifecycle

The clean, already-proven case is the one where Mintarr owns the whole path (its own lanes, today). Extending that to operator-routed completed folders keeps the same ownership shape, **after the completed-folder connector copies the source into Mintarr's managed output mount** — once files are under Mintarr's output path, the existing Lidarr ManualImport flow ([PR #32 lineage](../../development/AGENT_HANDOVER.md)) is reused. Two code realities follow (see Implementation notes): Phase 4 must preserve or parameterize the current `/output/<jid>` → Lidarr-visible path mapping, and queue cleanup stays best-effort for Mintarr-owned `downloadId`s only. The universal gate breaks this shape — for Lidarr-grabbed content, Lidarr is the grabber and tracker, so inserting Mintarr means reconciling Lidarr's queue for *every* external grab and resolving torrent-vs-usenet asymmetry (Mintarr is a SAB client to Lidarr, not a torrent client). Deferring that is a risk decision, not a loss of ambition.

### Discovery is high-value and cheap; conflating it with a guarantee is the trap

Reading Lidarr's client config to *infer* "which folders should Mintarr watch?" is a genuine onboarding win and stays read-only on Lidarr's side (Mintarr has no `/downloadclient` or `/remotepathmapping` client code today — that is new, additive work). The failure mode to avoid is letting that discovery imply coverage it does not provide ("Mintarr knows about my qBit, therefore my qBit imports are safe"). Locking discovery as *mapping assistance only* prevents that false sense of safety.

### It keeps the roadmap coherent

`sab_usenet` and `qbittorrent_torrent` already exist in the connector model as completed-folder ingest connectors. This ADR confirms that framing is the *mechanism* and draws the line at where it stops, so Phase 4 work proceeds without re-litigating scope.

## Consequences

### Positive

- Every Mintarr-routed source is QC-gated under one shared policy; coverage grows by adding completed-folder source connectors, no new architecture.
- Read-only completed-folder ingest preserves seeding and never fights the operator's download clients.
- Import-status ownership is unambiguous: Mintarr owns it for routed sources, Lidarr owns it for everything else.
- Lidarr-config discovery makes onboarding fast without crossing ADR-0008's boundary.
- The hard, entangled universal-gate work is sequenced after the simpler model proves out.

### Negative / accepted trade-offs

- **Coverage is opt-in, not automatic.** Content Lidarr grabs and imports itself is *not* QC'd unless the operator routes that category to Mintarr. This must be documented prominently so operators do not assume blanket coverage.
- Operators who want "QC everything Lidarr does" will not get it in this phase. That is a deliberate deferral with a re-evaluation path, not a rejection.
- A clear per-source operator-config step is required (route category → Mintarr; stop Lidarr importing that folder). The dashboard/connector UI must make this legible.

## Alternatives considered

### Alternative 1: Universal gate now (QC everything Lidarr imports)

Rejected for now. Requires Mintarr to own Lidarr's grab→import lifecycle for content Lidarr itself grabs — via either becoming the download client Lidarr talks to, or intercepting every external grab's import with queue reconciliation. Brings torrent/usenet asymmetry, category mapping, seeding semantics, and import-status split-brain. Strategically right as a *later* ambition; too risky before the scoped model is stable.

### Alternative 2: Mintarr as a universal download-client proxy Lidarr talks to

Rejected. Cleanest lifecycle handshake (Lidarr thinks Mintarr is its client), but it is the download-client-management role [ADR-0008](0008-strategic-positioning.md) explicitly keeps in Lidarr's territory, and it is asymmetric (Mintarr is SAB-compatible, not a torrent client), so it cannot route Lidarr's torrent grabs anyway. High complexity for partial coverage.

### Alternative 3: Status quo — only Mintarr's own fetch lanes

Rejected as too narrow. The completed-folder ingest connectors are low-risk and clearly in scope (source coverage + import safety), and operators legitimately want to route qBit/SAB categories through Mintarr's QC. Declining that would under-serve the core positioning.

## Re-evaluation triggers

Re-open (likely as a successor ADR for the universal gate) when:

1. **The scoped model is stable in real operation** — completed-folder ingest connectors (sab_usenet, qbittorrent_torrent) are shipped and proven, and import-status ownership has held up without reconciliation bugs.
2. **Operators consistently report** that routing categories manually is insufficient and they need Lidarr's own grabs gated automatically.
3. **A clean lifecycle handshake is demonstrated** for intercepting Lidarr-grabbed content (queue reconciliation + seeding + torrent/usenet handling) that does not require Mintarr to manage download clients in violation of [ADR-0008](0008-strategic-positioning.md).

Until then, ADR-0012 stands: scoped gate now, universal gate as a future, separately-decided phase.

## Implementation notes (for Phase 4)

These are feasibility constraints surfaced by a repo-grounded review ([issue #53](https://github.com/eivindsjursen-lab/mintarr/issues/53)); they bind the `sab_usenet` / `qbittorrent_torrent` design, not this ADR's decision:

- **Path mapping.** `server.py` currently hardcodes Lidarr's import folder (`/downloads/TidalHiRes/complete/<jid>`) and string-rewrites ManualImport paths to `/output/`. Phase 4 must preserve or parameterize the `/output/<jid>` → Lidarr-visible mapping rather than assume it.
- **Connectors copy, then enqueue normal jobs.** Completed-folder connectors must enqueue ordinary `source_grab` pipeline jobs and copy the source into Mintarr-managed work/output — they must **not** call Lidarr ManualImport directly from adapter code. This is what keeps the QC invariant enforced for these lanes.
- **Use the Soulseek validation pattern, not LocalFolder's.** qBit/SAB completed folders need the Soulseek-style settle window + partial-marker rejection, not LocalFolder's lighter manual-drop assumption.
- **Queue cleanup is Mintarr-owned only.** `_cleanup_lidarr_queue()` removes queue rows by `downloadId == jid`; an operator-routed external grab may have no such row, and Mintarr must not try to own the external client's queue row (that would be universal-gate territory).
- **New Lidarr client helpers.** Discovery needs `/api/v1/downloadclient` + `/api/v1/remotepathmapping` client methods plus client-specific `fields[]` parsing; none exist today.
- **Manifest semantics.** Per ConnectorManifest v1, `dry_run` is verifier-only; these source connectors remain `disabled` / `import`, matching current config semantics.

## Out of scope for this ADR

- **Implementation** of the sab_usenet / qbittorrent_torrent connectors (that is Phase 4 work against this scope).
- **The exact Lidarr-discovery UX** (which API fields, how the mapping wizard surfaces in the dashboard) — a design-doc concern.
- **Managing or configuring download clients** — remains Lidarr's territory per [ADR-0008](0008-strategic-positioning.md).
- **Output targets beyond Lidarr** — covered by the Output connector concept, not here.

---

> Locked: 2026-06-03
