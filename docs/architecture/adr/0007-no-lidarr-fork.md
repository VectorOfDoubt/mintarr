# ADR-0007: Mintarr will not fork Lidarr

**Status:** Accepted — locked 2026-05-26
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0008 Strategic positioning](0008-strategic-positioning.md), [ADR-0001 Rename from tidalhires](0001-rename-from-tidalhires.md)

---

## Context

During Mintarr's foundation phase, the question came up whether Mintarr should fork Lidarr (`Lidarr/Lidarr`, ~150,000 LOC C# / .NET 6, GPL-3.0) instead of continuing as a companion application. The motivations for a fork were:

- single-product installation (no Lidarr + Mintarr sandwich)
- ability to fix Lidarr's known multi-album release matching weaknesses in the core
- native tag-writing through Lidarr's existing infrastructure
- unified database and UI

The question is strategically significant because the answer determines whether Mintarr remains a focused pre-import QC layer or expands its scope to include library management.

## Decision

**Mintarr will not fork Lidarr.** Mintarr is and remains a companion application to Lidarr.

This decision is intended to be durable. It is re-evaluated only under the specific triggers listed below.

## Rationale

### Maintenance cost asymmetry

A Lidarr fork would carry approximately 5x the annual maintenance burden of Mintarr-as-companion for the same functional outcome. Concretely:

| Component | Why it costs us |
|---|---|
| C# / .NET 6 codebase (150k LOC) | Our expertise is Python; ramp-up alone is months |
| React + Redux frontend (30-40k LOC) | We have deliberately chosen vanilla HTML/CSS/JS for Mintarr |
| MusicBrainz integration | Complex metadata model, not our domain |
| Indexer + download-client protocols | Newznab + 4+ download-client APIs to maintain |
| Profile + Custom Format system | Working and complex; no value in re-implementation |
| Upstream merge work | 1-3 days per Lidarr release, 1-2 releases per month |
| Multi-platform builds | .NET MSI/DMG/Docker/ARM builds we don't need today |
| Discovery + community split | Forks compete with upstream for SEO and trust |

Estimated maintenance year 1: 500-1000 hours for a fork. ~100-200 hours for companion. The factor is consistent across all forecasting models we ran.

### Pattern of arr-stack forks

Forks in the arr-stack ecosystem succeed when they fill a real gap (Sonarr → Lidarr filled the music gap, Jackett → Prowlarr filled centralised indexer-management). Forks that position themselves as "same app, but better" have historically failed to take meaningful adoption.

A Mintarr-as-Lidarr-fork is in the second category. Its differentiator (better QC) does not require library-management ownership.

### Boundary of value

Mintarr's defensible value sits entirely outside Lidarr's core competence:

- pre-import QC (ffprobe, flac -t, FLAC Detective, future CUETools/CTDB)
- multi-source orchestration (TIDAL, Local, Soulseek, future SAB/qBit)
- policy and scoring (V2 verification, REVIEW_REQUIRED workflow)
- audit trail and operator UI

Lidarr's competence (artist/album library, MusicBrainz model, wanted/missing, indexer ecosystem, profiles, custom formats, import targets) is well-developed and actively maintained. Owning both is not an upgrade — it is scope explosion.

### Multi-album / release matching is not a fork problem

The original motivation included Lidarr's well-known multi-album release matching weakness ("Album match is not close enough: 70.1% vs 80%"). Forking Lidarr would not automatically solve this because the root causes are:

- MusicBrainz release / release-group data quality
- edition / deluxe / remaster / anniversary variants
- tracklist mismatch between physical and digital releases
- existing-library state and import-mode interaction

A fork would inherit all four. The leverage Mintarr does have (release-family matching, track-count similarity, edition-aware import policy, dashboard explanation) lives in our layer and does not require touching Lidarr internals. This work is tracked as F5.1 in the roadmap.

## Consequences

### Positive

- Mintarr keeps a small, focused codebase that the maintainer team can actually maintain
- Mintarr can co-exist with any maintained Lidarr version through a multi-version Lidarr client (`LIDARR_INTEGRATION.md` spec)
- Mintarr stays attractive as a contribution target — its scope is comprehensible to new contributors in a single afternoon
- Mintarr can later add other Output connectors (Plex direct, Jellyfin direct, library-only) without inheriting library-management responsibility

### Negative

- Multi-album release matching cannot be solved at the deepest level (root cause is in Lidarr + MusicBrainz); Mintarr can mitigate but not eliminate
- Users continue to install and configure two applications (Lidarr + Mintarr)
- Mintarr is dependent on Lidarr remaining functional; if Lidarr becomes unmaintained, the dependency is a risk

### Accepted trade-offs

- Tag-writing (Picard / Beets) is intentionally deferred until a separate ADR establishes Lidarr tag-ownership boundaries
- "Better release-family matching" becomes a Mintarr feature (F5.1) rather than a Lidarr patch
- Custom Format coordination remains a documentation responsibility (`CONFIGURATION.md` will document recommended CF scores per Mintarr source)

## Alternatives considered

### Alternative 1: Full Lidarr fork

Rejected for the reasons above. Cost asymmetry and pattern of failed "same but better" forks dominate.

### Alternative 2: Fork Lidarr only for pre-import hooks

Rejected. A fork narrowed to pre-import hooks still carries the full maintenance cost of every other subsystem (the merge burden does not scale linearly with fork-surface). The same outcome is achievable via Alternative 3 at 1% of the cost.

### Alternative 3: PR a pre-import webhook into Lidarr upstream

Deferred but not rejected. A small upstream PR adding a pre-import event hook would let Mintarr "approve / reject" imports without owning Lidarr's pipeline. This is documented as a future option pending:

- Mintarr v1.0 release (we need real adoption numbers before approaching Lidarr maintainers)
- Lidarr v4 stabilisation (the rewrite changes the pipeline shape)
- A concrete RFC-style proposal aligned with Lidarr maintainer preferences

### Alternative 4: Generalise Mintarr to standalone (no Lidarr dependency)

Partially adopted. The Connector architecture (Codex's `CONNECTOR_PLUGIN_ARCHITECTURE.md`) already treats `lidarr_manual_import` as one OutputConnector among potential others (`plex_direct`, `jellyfin_direct`, `library_filesystem_only`). This is a future feature path, not a replacement for the companion model.

### Alternative 5: Integrated app / Lidarr replacement

Strategic option only, not adopted.

The long-term ideal product might be a single application where Lidarr's
library-management model and Mintarr's quality-evidence model are integrated in
one coherent workflow. That could be a Lidarr fork, a Lidarr-native plugin model,
or a new application that reuses none of Lidarr's implementation language or UI.
It could also address Lidarr's "multiple releases of the same album" weakness at
the correct layer: the same component that chooses the target release/edition
would also own the quality decision and import, instead of Mintarr having to
infer or temporarily switch Lidarr state from the outside.

This remains a future ADR, not a Phase 0-7 commitment, because it changes the
product category. Mintarr would stop being only a QC gate and would become a
music library manager. That implies owning wanted/missing state, MusicBrainz
metadata, release selection, download-client coordination, imports, rescans,
library migrations, API compatibility, and a substantially larger UI.

The current companion architecture should therefore be pushed to its natural
limit first. If the Newznab/SAB/API surfaces prove unable to provide the safety
guarantees Mintarr needs, the next escalation is:

1. upstream Lidarr hooks or a Lidarr-native plugin/RFC;
2. a narrow integration layer that passes explicit `albumId`, release context,
   and operator intent to Mintarr;
3. only then a separate ADR evaluating an integrated app, fork, or replacement.

Evidence for that ADR must be concrete: recurring production failures that
cannot be made safe with the companion model, rejected upstream/plugin paths, and
enough adoption to justify owning a full library manager.

## Re-evaluation triggers

This ADR is durable. It is re-opened only if one of the following occurs:

1. **Lidarr becomes formally unmaintained.** Concretely: no commit on `Lidarr/Lidarr` main branch for 12 consecutive months, OR the maintainer team publicly archives the repository.
2. **Mintarr reaches scale where ownership of the full stack is necessary.** Concretely: >10,000 active installs and consistent user demand that requires library-management features Mintarr cannot deliver as a companion.
3. **Lidarr maintainers reject the pre-import webhook PR (when filed) AND a follow-up RFC is also rejected**, AND the rejection rationale makes Mintarr's companion model untenable in practice.
4. **The companion model repeatedly fails hard safety guarantees even with conservative resolvers and holds.** Concretely: production dogfood shows that "everything through Mintarr's QC gate" cannot be made reliable through Newznab/SAB/API integration without native Lidarr context such as `albumId`.

Until then, ADR-0007 stands. Any contribution proposing fork-shaped scope expansion should be closed with a reference to this ADR.

---

> Locked: 2026-05-26
