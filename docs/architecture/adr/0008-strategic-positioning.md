# ADR-0008: Mintarr's strategic positioning

**Status:** Accepted — locked 2026-05-26
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0007 No Lidarr fork](0007-no-lidarr-fork.md), [ADR-0003 Connector vs Adapter](0003-connector-vs-adapter.md)

---

## Context

After deciding not to fork Lidarr (ADR-0007), the question of what Mintarr *is* became more pressing than what Mintarr is *not*. Without a sharp positioning statement, every future feature proposal becomes a scope debate:

- "Should Mintarr track wanted/missing?"
- "Should Mintarr have its own indexer config?"
- "Should Mintarr write tags?"
- "Should Mintarr maintain a library?"

In practice, every "yes" to questions like these pulls Mintarr toward Lidarr's territory and erodes the cost asymmetry that justified ADR-0007 in the first place. We need a positioning statement that makes these answers obvious without re-litigating the boundary every time.

## Decision

**Mintarr is the quality control and import orchestration layer that Lidarr lacks. It is not a better Lidarr.**

This positioning is locked. All feature decisions, documentation framing, marketing copy, and contributor onboarding flow from it.

## Mintarr's core competence (what we own)

Mintarr owns these capabilities. Investment, optimisation and feature work here is in scope by default:

- **Pre-import quality control.** ffprobe codec gate, flac -t integrity, FLAC Detective spectral analysis, future CUETools/CTDB verification.
- **Source adapter abstraction.** A versioned `SourceAdapter` Protocol that lets community contributors add new sources (TIDAL, Local, Soulseek, future SAB/qBit/CD-rip/YouTube) without touching the pipeline.
- **Verifier adapter abstraction.** A connector model for evidence-producing tools (ffprobe, flac -t, FLAC Detective, CUETools, beets/Picard/AcoustID) that distinguishes hard gates from spectral heuristics from metadata-identity tools.
- **Policy and scoring.** V2 verification decisions (ACCEPT / ACCEPT_PROVISIONAL / REVIEW_REQUIRED / BLOCK), score windows, completeness rules, source-aware thresholds.
- **REVIEW_REQUIRED workflow.** Operator-facing queue of items the policy could not auto-decide. First-class concept, not an afterthought.
- **Audit trail.** `decisions.jsonl`, sidecar `verification.json`, action log. Every decision is reviewable and every action is attributable.
- **Operator dashboard.** The "mission control" UI for quality, imports, review, errors, policy state and connector health.
- **Safe import to outputs.** Lidarr ManualImport today; potentially Plex / Jellyfin / Roon / filesystem-only later. Output connector is a first-class concept.

## What Mintarr does not own (Lidarr's territory)

Mintarr does *not* own these capabilities. Feature proposals here are out of scope by default. The path forward when an operator needs them is "use Lidarr" — Mintarr stays the QC and orchestration layer:

- **Artist / album library management.** Tracking what's in your library, what's missing, what's wanted, calendar of upcoming releases.
- **MusicBrainz model.** Release groups, releases, recordings, works. Lidarr already does this well.
- **Indexer ecosystem.** Newznab/Torznab indexer configuration, profiles, priority. Mintarr exposes itself *as* a Newznab indexer to Lidarr but does not manage other indexers.
- **Download client ecosystem.** SAB / Transmission / qBittorrent / Deluge / NZBGet configuration. Mintarr exposes itself *as* a SAB-compatible download client to Lidarr but does not configure others.
- **Profiles and Custom Formats.** Quality profiles, custom format rules, language profiles. Mintarr produces source-tagged titles that Lidarr's CF system can score, but does not manage Lidarr's profile configuration.
- **Import targets.** Where files end up on disk after import. That is Lidarr's job (or whichever Output connector is configured).
- **Tag writing.** Modifying file metadata. Out of scope until a separate ADR establishes Lidarr tag-ownership boundaries.

## Boundary test for new features

When a feature is proposed, apply this test in order:

1. **Does the feature improve QC accuracy, source coverage, verifier evidence, policy precision, audit clarity, or import safety?** → In scope. Default-accept subject to design review.
2. **Does the feature improve operator experience of Mintarr's existing scope (dashboard, REVIEW_REQUIRED workflow, configuration, observability)?** → In scope.
3. **Does the feature require Mintarr to start tracking, managing or owning something Lidarr already owns (library, MusicBrainz model, indexer config, profiles, import targets)?** → Out of scope. The answer is either "use Lidarr for that" or "send a PR to Lidarr upstream".
4. **Does the feature reduce Lidarr coupling without expanding Mintarr's library-management scope?** → In scope. Adding new OutputConnectors (Plex, Jellyfin, filesystem) is in scope; replicating Lidarr's library is not.

Edge cases are resolved by referring to this ADR. A contributor who disagrees with a scope rejection can propose a successor ADR; they cannot get the boundary moved by adding the feature incrementally.

## Consequences

### Positive

- Feature proposals get fast, predictable answers
- Codebase stays small and learnable for new contributors
- Mintarr remains attractive as a focused tool, not as a Lidarr replacement
- Documentation can be sharp: `VISION.md`, `README.md` and `COMPARISON.md` all reference this ADR rather than re-deriving the positioning
- Roadmap stays coherent: every planned feature is checkable against the boundary test

### Negative

- Some operator pain remains visible — users will hit Lidarr's multi-album release matching issues and want Mintarr to fix them in the core. Mintarr will mitigate (F5.1 release-family matching) but cannot eliminate.
- Mintarr cannot stand alone today; it depends on Lidarr being functional. Output-connector generalisation (Plex / Jellyfin / filesystem) reduces this dependency over time but does not remove it in v1.

### Accepted trade-offs

- "Single product" is sacrificed for "right tool for the job"
- We will say "no" to feature requests that crossed the boundary, even when they would be popular
- We will document the boundary so visibly that the "no" is predictable and the contributor can plan accordingly

## What the positioning is *not*

To prevent misreading, three explicit clarifications:

1. **Not a value judgement on Lidarr.** Lidarr is a well-designed library manager. Mintarr exists because it fills a different need, not because Lidarr is bad.
2. **Not a Lidarr-only tool.** Mintarr's OutputConnector model supports any import target. Lidarr is the first and primary, not the only.
3. **Not a static boundary.** The boundary test exists so we can re-evaluate per-feature, not so we can refuse all evolution. New ADRs can adjust the boundary; this ADR is the starting point.

## Re-evaluation triggers

This ADR is re-opened only if one of the following occurs:

1. **A contributor proposes a feature that this ADR cannot give a clear answer for.** That gap is worth a successor ADR.
2. **Output coverage expands enough that "QC and orchestration" no longer describes Mintarr accurately.** For example: Mintarr is the default music-import-orchestrator across Plex/Jellyfin/Roon/Lidarr/filesystem with comparable adoption to Lidarr itself. At that point the positioning may legitimately broaden.
3. **ADR-0007 is re-opened and a fork is approved.** The positioning would need to be rewritten entirely.

Until then, ADR-0008 stands. The positioning statement at the top of this document is reproduced verbatim in `README.md`, `VISION.md` and `COMPARISON.md`.

---

## Amendment 2026-06-07: read-only library quality evidence (F5.4)

**Status:** Accepted. Extends — does not reopen — the positioning above.

F5.4 lets Mintarr **measure** the existing Lidarr library (read-only) and compare a
candidate against that *measured* quality instead of trusting Lidarr's quality
label. This clarifies one line of the boundary:

- **In scope:** Mintarr may hold a **read-only quality evidence register** over
  Lidarr-owned files — measuring codec/bit-depth/sample-rate/integrity/spectral
  evidence and storing it — to make import decisions on measured truth. Mintarr
  owns *audio quality*; Lidarr remains the truth for *identity/ownership* (which
  artist/album/track exists).
- **Still out of scope (unchanged):** Mintarr does **not** own library
  management — no tag writing, no re-encoding, no moving/renaming/deleting
  library files, no maintaining wanted/missing or library structure. The
  register is observational only.

This is consistent with the boundary test ("does it duplicate Lidarr's job, or
fill the QC gap?") — measuring quality fills the QC gap; managing the library
would duplicate Lidarr. Decision use of measured evidence is opt-in
(`MINTARR_MEASURED_EXISTING`, default-off). See
[F5.4 library evidence index](../../design/F5.4_LIBRARY_EVIDENCE_INDEX.md).

---

> Locked: 2026-05-26 · Amended: 2026-06-07 (F5.4)
