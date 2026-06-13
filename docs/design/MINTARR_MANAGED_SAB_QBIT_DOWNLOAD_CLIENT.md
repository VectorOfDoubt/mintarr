# Mintarr-managed SAB/qBittorrent download-client lane

> **Type:** Design document
> **Version:** 0.1 - 2026-06-13
> **Status:** Proposed
> **Related:** [ADR-0012 QC import-gate scope](../architecture/adr/0012-qc-import-gate-scope.md), [ADR-0008 strategic positioning](../architecture/adr/0008-strategic-positioning.md), [Download client category gate](DOWNLOAD_CLIENT_CATEGORY_GATE.md), [Phase 4 SAB/qBit completed ingest](PHASE4_SAB_QBIT_COMPLETED_INGEST.md), [Lidarr integration](../specs/LIDARR_INTEGRATION.md)

## 1. Problem

Mintarr already exposes two safe SAB/qBit-adjacent mechanisms:

1. **Mintarr-owned lanes** where Lidarr talks to Mintarr as the download client
   and Mintarr owns fetch -> QC -> ManualImport.
2. **Completed-folder ingest** where the operator points Mintarr at a dedicated
   completed music folder and explicitly routes that folder into Mintarr.

Those mechanisms do not solve the common Arr-stack topology where Lidarr uses
shared SABnzbd/qBittorrent clients directly:

```text
Lidarr ----> SAB/qBit ----> completed music folder ----> Lidarr auto-import
Sonarr ----> same client
Radarr ----> same client
```

In that shape, Mintarr cannot safely watch the shared completed root and cannot
guarantee QC before import. Lidarr owns the external-client queue entry and may
auto-import as soon as the download completes. If Mintarr also watches the same
folder, import-status ownership splits between Lidarr and Mintarr.

The local dogfood environment demonstrated the risk: the qBittorrent completed
folder contained TV/movie material from other Arr applications and no dedicated
music category. Dogfooding that root would validate the wrong system.

## 2. Decision

For automatic Lidarr-triggered SAB/qBit music, the robust architecture is:

> **Lidarr talks to Mintarr; Mintarr talks to SAB/qBit.**

Mintarr becomes the Lidarr-facing download client for the music lane. SABnzbd
and qBittorrent become backend transfer engines controlled by Mintarr for a
dedicated music category/path.

This is stronger than completed-folder ingest:

- Completed-folder ingest is **operator-routed**: the operator gives Mintarr a
  folder to inspect.
- Mintarr-managed SAB/qBit is **Lidarr-routed through Mintarr**: Lidarr adds the
  release to Mintarr, Mintarr submits the backend SAB/qBit job, Mintarr reports
  queue/history back to Lidarr, and Mintarr imports only after QC.

In short:

```text
Lidarr Newznab search
  -> Lidarr AddUrl to Mintarr download-client endpoint
  -> Mintarr submits SAB/qBit backend job in category "mintarr-music"
  -> Mintarr monitors backend completion
  -> Mintarr copies completed files into managed work/output
  -> shared QC
  -> Lidarr ManualImport only if accepted
```

This design does **not** make Mintarr own the whole download-client ecosystem.
Mintarr owns only the configured music category that it submits jobs into.
Sonarr/Radarr categories remain outside Mintarr.

## 3. Invariants

These invariants are binding for implementation:

- **All automatic music imports in this lane pass through Mintarr QC.** Lidarr
  must not directly auto-import from the backend category.
- **The backend category is dedicated to Mintarr music.** It must not be shared
  with Sonarr, Radarr, manual TV/movie downloads, or generic completed folders.
- **Mintarr is the queue owner for this lane.** Lidarr sees Mintarr download IDs,
  not raw SAB/qBit IDs. Mintarr maps `jid <-> backend_job_id`.
- **Backend files are read/copy sources.** Mintarr copies into its managed
  work/output path before QC; it does not run QC in place in the backend
  completed root.
- **No adapter calls Lidarr ManualImport directly.** The normal source-grab
  pipeline owns ManualImport.
- **Cancel/remove/blocklist propagates.** Lidarr queue actions against the
  Mintarr download ID must cancel/remove the backend job where safe and preserve
  Mintarr's blocklist/album-hold semantics.
- **Seeding is not broken by default.** qBittorrent jobs must not be deleted or
  moved in a way that destroys seeding unless the operator explicitly opts into
  that cleanup policy.
- **Shared paths fail closed.** If Mintarr cannot prove the category/path is
  dedicated enough to own, the lane stays disabled.

## 4. Relationship to existing designs

### 4.1 ADR-0012

[ADR-0012](../architecture/adr/0012-qc-import-gate-scope.md) deliberately
rejected a universal gate for v1 because Mintarr did not yet own the external
client lifecycle. This design is a successor mechanism for one safe subset:
music jobs that Lidarr routes through Mintarr first.

It does not intercept arbitrary Lidarr-owned SAB/qBit downloads.

### 4.2 Download client category gate

[Download client category gate](DOWNLOAD_CLIENT_CATEGORY_GATE.md) remains valid
for completed-folder ingest. That design answers:

> "Which folders may Mintarr watch?"

This design answers a different question:

> "How can Lidarr use SAB/qBit while Mintarr still owns the import lifecycle?"

The answer is not "watch the shared folder". The answer is "make Mintarr the
download client Lidarr talks to for that music lane".

### 4.3 ADR-0008

[ADR-0008](../architecture/adr/0008-strategic-positioning.md) says Mintarr does
not configure the user's download-client ecosystem. This design keeps that
boundary:

- Mintarr may submit, monitor, and cancel backend jobs it created.
- Mintarr does not manage Sonarr/Radarr categories.
- Mintarr does not rewrite global SAB/qBit configuration.
- Operator onboarding validates configuration; it does not silently change it.

## 5. Runtime model

### 5.1 Queue identity

Mintarr needs a durable table or sidecar state for backend jobs:

| Field | Meaning |
|---|---|
| `jid` | Mintarr worker/download ID exposed to Lidarr |
| `source_type` | `sab_usenet_backend` or `qbittorrent_backend` |
| `backend_job_id` | SAB NZO ID or qBittorrent hash |
| `category` | Dedicated category, e.g. `mintarr-music` |
| `target_album_id` | Lidarr album ID when known |
| `release_title` | Search result title shown to Lidarr |
| `state` | queued/downloading/completed/importing/review/failed/cancelled |
| `backend_path` | Backend completed path, secret-safe in logs/UI |
| `created_at`, `updated_at` | Audit and recovery |

The queue/history endpoints Lidarr calls should be backed by Mintarr state.
Backend state is input evidence, not the public identity Lidarr relies on.

### 5.2 Backend submit

Mintarr submits jobs to SAB/qBit with:

- configured backend URL/API key;
- dedicated category;
- release URL or magnet/NZB payload from the candidate source;
- optional save path only if the backend supports it safely;
- paused/start policy matching operator config.

Submission failure returns a failed Mintarr job to Lidarr; it must not fall back
to a direct Lidarr backend grab.

### 5.3 Backend monitor

Mintarr polls backend job status and maps it to the SAB-compatible queue/history
shape Lidarr expects.

Important differences:

- SAB jobs usually finish into a category folder and can be removed after
  import.
- qBittorrent jobs may need to remain for seeding. Mintarr must support
  "leave backend job seeding" as the default-safe policy.

### 5.4 Completion and ingest

When the backend job is complete:

1. Mintarr resolves the completed path for the specific backend job.
2. Mintarr enforces category/path containment.
3. Mintarr waits for settle.
4. Mintarr copies into `DOWNLOAD_BASE/<jid>`.
5. Mintarr runs the normal source-grab pipeline.
6. Mintarr imports through Lidarr ManualImport only if QC accepts or the
   operator promotes.

This is the Soulseek completed-folder safety model, but the trigger and queue
identity are Mintarr-managed instead of operator-posted.

## 6. Configuration contract

Suggested future configuration:

```text
MINTARR_SAB_BACKEND_ENABLED=false
MINTARR_SAB_BACKEND_URL=
MINTARR_SAB_BACKEND_API_KEY=
MINTARR_SAB_BACKEND_CATEGORY=mintarr-music
MINTARR_SAB_BACKEND_DOWNLOAD_ROOT=

MINTARR_QBIT_BACKEND_ENABLED=false
MINTARR_QBIT_BACKEND_URL=
MINTARR_QBIT_BACKEND_USERNAME=
MINTARR_QBIT_BACKEND_PASSWORD=
MINTARR_QBIT_BACKEND_CATEGORY=mintarr-music
MINTARR_QBIT_BACKEND_DOWNLOAD_ROOT=
MINTARR_QBIT_BACKEND_CLEANUP=leave_seeding
```

The exact env names are placeholders; implementation should follow existing
connector naming conventions.

The category is not cosmetic. It is the ownership boundary.

## 7. Onboarding validation

Before enabling, dry-run must verify:

### Hard failures

- backend URL/key invalid;
- category missing;
- configured category path not mounted in Mintarr;
- category path resolves to `/`, a home directory, or another broad unsafe root;
- category path is shared with another known category in the same backend;
- Mintarr cannot create a test job or query a real job without touching global
  backend state;
- Lidarr is still configured to import directly from the same backend category
  when that can be detected.

### Warnings

- category is empty, so live shape cannot be proven yet;
- path is mounted read-write instead of read-only;
- category name is generic (`music`, `complete`, `downloads`, `finished`);
- backend does not expose enough per-category path detail;
- qBittorrent cleanup is set to remove jobs that may still be seeding;
- remote path mapping from backend path to Lidarr-visible output is ambiguous.

The UI must phrase coverage precisely:

> Lidarr music routed through Mintarr backend category `mintarr-music` is gated.

It must not say:

> qBittorrent is protected.

## 8. Failure handling

| Failure | Behavior |
|---|---|
| Backend submit fails | Mintarr job fails; Lidarr sees failed download. |
| Backend job disappears | Mintarr job fails or moves to review with audit, depending on phase. |
| Backend job completes but path is unsafe | BLOCK/failed before import; never import from unsafe path. |
| QC returns REVIEW_REQUIRED | Keep Mintarr download visible/paused to Lidarr using the existing review-hold model. |
| Operator cancels from Lidarr | Cancel backend job if safe; create album-hold/blocklist according to existing semantics. |
| Operator discards in Mintarr | Blocklist exact release when possible; cleanup Mintarr state; backend cleanup follows configured policy. |
| Mintarr restarts | Reconcile backend jobs from persisted `jid <-> backend_job_id` state. |

## 9. Implementation slices

### Slice 1 - Design and onboarding contract

- Land this design.
- Cross-link roadmap, category-gate, and configuration docs.
- Decide whether SAB or qBittorrent is first. SAB is likely simpler because
  post-import cleanup is less constrained by seeding.

### Slice 2 - Backend client abstraction

- Add mockable SAB/qBit backend clients for submit/status/cancel.
- No Lidarr exposure yet.
- Unit-test category/path handling and secret redaction.

### Slice 3 - Mintarr-managed queue state

- Persist `jid <-> backend_job_id`.
- Add recovery/reconciliation.
- Add queue/history presentation from Mintarr state.

### Slice 4 - SAB-compatible addurl integration

- Allow Lidarr AddUrl to create a Mintarr backend job.
- Report backend progress through Mintarr's existing SAB-compatible endpoints.
- Ensure cancel/remove/blocklist propagates to backend.

### Slice 5 - Completion ingest

- Resolve completed backend path.
- Copy through the shared completed-folder safety model.
- Enqueue normal source-grab pipeline.

### Slice 6 - Dashboard onboarding and coverage display

- Show enabled backend lanes, categories, path validation, and coverage
  semantics.
- Make "direct Lidarr SAB/qBit imports are not gated" visible.

### Slice 7 - Dogfood

- Create a real dedicated backend category.
- Disable direct Lidarr auto-import for that category.
- Run one SAB/qBit music release end-to-end:
  search -> grab -> backend download -> QC -> review/import -> cleanup.

## 10. Non-goals

- No watching shared SAB/qBit completed roots.
- No global SAB/qBit configuration editor.
- No Sonarr/Radarr integration.
- No guarantee that Lidarr's direct, non-Mintarr download clients are gated.
- No automatic unblocking of Lidarr blocklist entries.
- No auto-cleanup that breaks torrent seeding.

## 11. Open questions

1. Should SAB be implemented before qBittorrent because cleanup semantics are
   simpler?
2. Can Lidarr be configured so music uses only Mintarr as download client while
   SAB/qBit remain available to Sonarr/Radarr?
3. Should one backend category (`mintarr-music`) cover all Mintarr-routed music,
   or should categories be source-specific?
4. How much backend cleanup should Mintarr perform by default for qBittorrent?
5. ~~Should this successor design eventually become an ADR amendment to
   [ADR-0012](../architecture/adr/0012-qc-import-gate-scope.md), or stay a
   Phase 4 design until dogfooded?~~ **Resolved:** the boundary is locked
   before backend-client code in
   [ADR-0014](../architecture/adr/0014-mintarr-managed-download-backend.md)
   (the scoped successor ADR-0012's re-evaluation trigger #3 anticipated).

## 12. Review focus

Claude/reviewer should check:

1. Does the design solve Lidarr auto-import ownership without making Mintarr a
   universal download-client manager?
2. Are the invariants strong enough to guarantee "all automatic music imports
   in this lane pass through Mintarr"?
3. Is the SAB/qBit distinction, especially torrent seeding, handled safely?
4. Are the onboarding hard failures too strict or too loose?
5. Is this still compatible with ADR-0008 and ADR-0012, or does it need a
   successor ADR before implementation?
