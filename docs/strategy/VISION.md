# Mintarr — Vision

> **Type:** Strategy
> **Version:** 1.0 — 2026-05-26
> **Status:** Locked — derived from ADR-0007 and ADR-0008
> **Audience:** Anyone deciding whether Mintarr is the right tool for them, or considering contributing.

---

## What Mintarr is

> Mintarr is the quality control and import orchestration layer that Lidarr lacks. It is not a better Lidarr.

This positioning is locked. The reasoning lives in [ADR-0008 Strategic positioning](../architecture/adr/0008-strategic-positioning.md); this document is the public-facing translation.

Concretely, Mintarr sits between the places music comes from (TIDAL, Soulseek, local folders, future SAB/qBit/CD-rip lanes) and the place music goes to (Lidarr today, potentially Plex / Jellyfin / Roon / filesystem-only in the future). Everything that flows through Mintarr is verified, scored, and either auto-imported, sent for operator review, or blocked — with a complete audit trail at every step.

## What problem Mintarr solves

Self-hosted music import tooling has historically optimised for *finding* and *fetching* music. Verifying that what arrived is actually what was wanted has been an afterthought.

That is the gap Mintarr exists to fill. Specifically:

- **Fake high-resolution audio** (FLAC files that are actually upsampled MP3s, or AAC-in-MP4 mislabelled as FLAC) reach libraries because no tool in the chain runs codec verification before import.
- **Multi-source pipelines** (TIDAL + Soulseek + CD-rips + local files) require the same QC standard, but every tool implements its own (incompatible, inconsistent) pre-checks.
- **Quality decisions need to be reviewable.** When import fails, operators want to see *why*, not just that it failed. Sidecar evidence and audit trails are the answer.
- **Edition / release-family ambiguity** (deluxe / remaster / anniversary / regional variants) confuses Lidarr's import matching. Mintarr can mitigate this without owning library state.

Mintarr addresses these by treating every import as a pipeline of four named phases — `download_raw` → `normalize_audio` → `verify` → `import` — with explicit invariants between phases and a versioned `SourceAdapter` contract for sources.

## Who Mintarr is for

Three concrete audiences. Mintarr is built for these and prioritises decisions accordingly.

### Self-hosted music hoarders running Lidarr

The primary user. Has a Lidarr install, a TIDAL or Qobuz subscription, a Soulseek client, a folder of CD-rips, or some combination. Wants imports they can trust without manual spot-checking. Already runs the arr-stack and is comfortable with Docker Compose and reverse proxies.

### Audiophile-leaning library curators

Cares enough about FLAC authenticity to inspect spectra and reject upsampled material. Wants a tool that automates that judgement consistently and explains its decisions. May already use FLAC Detective, spek, or Audiowand manually.

### Contributors and tool builders

Wants to add a source (a new streaming service, a new download client) or a verifier (a new spectral analyser, a metadata identity tool like beets/Picard/AcoustID) and have it slot into a stable contract without re-architecting Mintarr. The versioned adapter and connector contracts exist for these contributors.

## What Mintarr is not for

- **People who want a Lidarr replacement.** Mintarr does not manage your library, track wanted/missing, or talk to MusicBrainz. Use Lidarr for that.
- **People who want streaming-service-DRM circumvention.** Mintarr verifies what arrives; it does not bypass content protection or violate terms of service.
- **People who want a tag editor.** Tag writing is out of scope until a separate ADR establishes Lidarr tag-ownership boundaries.
- **People who want a download client.** Mintarr exposes itself *as* a SAB-compatible download target to Lidarr, but it does not manage SAB / qBittorrent / Transmission for you.

These are not future features waiting to be built. They are deliberate scope exclusions. The [boundary test in ADR-0008](../architecture/adr/0008-strategic-positioning.md) records why.

## What Mintarr owns

Investment, optimisation, and feature work in these areas is in scope by default:

- **Pre-import quality control orchestration.** Mintarr runs ffprobe (codec gate), `flac -t` (integrity), FLAC Detective (spectral analysis), and future CUETools/CTDB and beets/Picard/AcoustID. Mintarr **orchestrates** these tools and combines their evidence into a verification decision. Mintarr does **not** re-implement the tools themselves in v1 — they remain external dependencies. (Future direction: a small set of additive in-house sensors as Phase 8 "Mintarr Audio Lab"; see [ROADMAP.md](ROADMAP.md).)
- **Source adapter abstraction.** A versioned `SourceAdapter` Protocol that lets contributors add new sources without touching the pipeline.
- **Verifier adapter abstraction.** A connector model for evidence-producing tools — hard gates, spectral heuristics, and metadata-identity tools are different first-class categories.
- **Policy and scoring.** V2 verification decisions (ACCEPT / ACCEPT_PROVISIONAL / REVIEW_REQUIRED / BLOCK), source-aware thresholds.
- **REVIEW_REQUIRED workflow.** Operator queue for items the policy could not auto-decide.
- **Audit trail.** Every decision and every action is reviewable and attributable.
- **Operator dashboard.** Mission control for quality, imports, review, errors, policy state, connector health.
- **Safe import to outputs.** Lidarr today; Plex / Jellyfin / Roon / filesystem-only as the OutputConnector model is exercised.

## Non-goals

Things Mintarr will not do, no matter how popular the request:

- Artist / album library tracking, wanted / missing queues, calendar of upcoming releases
- MusicBrainz release-group / release / recording / work modelling
- Newznab / Torznab indexer configuration management
- Profile and quality-rule administration
- Download client (SAB / qBit / Transmission) configuration
- Tag writing
- Library file organisation (filesystem layout, renaming policies)

The [boundary test in ADR-0008](../architecture/adr/0008-strategic-positioning.md) gives a 4-step rule for accepting or rejecting feature proposals. Contributors and reviewers should apply it before opening or merging anything in these areas.

## What success looks like

For Mintarr to be considered successful, three things need to be true a year after public launch:

1. **An operator running TIDAL + LocalFolder + Soulseek imports through Mintarr does not need to manually spot-check FLAC authenticity.** The dashboard tells them what to look at; everything else flows through.
2. **A new source or verifier connector can be contributed by someone who has not previously worked on Mintarr, in a single weekend.** That includes reading the adapter contract spec, writing the adapter, writing tests, and getting the PR merged.
3. **Mintarr stays a small, focused codebase that two people plus AI assistance can maintain indefinitely.** Scope discipline is the load-bearing assumption.

Failing any of these would be a signal that the positioning needs re-evaluation.

## Relationship to Lidarr

Mintarr depends on Lidarr today. Lidarr does not depend on Mintarr.

That asymmetry is deliberate. Mintarr exposes itself to Lidarr as a Newznab indexer + SAB-compatible download client; Lidarr does not know Mintarr exists. This means:

- Existing Lidarr installs can adopt Mintarr without configuration changes on the Lidarr side beyond adding one indexer and one download client.
- Mintarr can be removed at any time without Lidarr noticing.
- Mintarr's evolution is constrained by Lidarr's stable surfaces, not Lidarr's internal refactors.

As the OutputConnector model is exercised, Mintarr will gain the ability to write to Plex / Jellyfin / filesystem-only outputs alongside Lidarr. The Lidarr dependency relaxes over time but is not removed in v1.

If Lidarr's status changes — formally unmaintained, or a hostile API break — [ADR-0007 §Re-evaluation triggers](../architecture/adr/0007-no-lidarr-fork.md) records the conditions under which Mintarr's relationship to Lidarr would be reconsidered.

## Documentation pace and contribution rhythm

Mintarr is built without a deadline. Quality of decisions, tests, and documentation comes before shipping speed. The [Documentation Index](../MINTARR_DOCUMENTATION_INDEX.md) tracks the planned document set; the [Roadmap](ROADMAP.md) tracks planned features and their order.

Contributions are welcome from anyone willing to read [CONTRIBUTING.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/CONTRIBUTING.md) and engage with the boundary test. The maintainer team prioritises holding the scope line over maximising contributor volume.

---

> Last updated: 2026-05-26
