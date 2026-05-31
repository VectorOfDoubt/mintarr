# How Mintarr compares to alternatives

> **Type:** Community / orientation
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Comparisons evolve as tools change.
> **Audience:** Operators evaluating Mintarr against existing tools.

---

## 1. Why this document exists

When people discover Mintarr, the natural question is "how is this different from X?". X is one of:

- Lidarr by itself
- Beets
- Picard
- soulseek-bridges
- The post-import scripts you wrote yourself

This document answers that question for each X. It is not a marketing brochure; it is a candid comparison so operators can choose the right tool.

For the underlying positioning ("Mintarr is not a better Lidarr"), see [VISION.md](../strategy/VISION.md) and [ADR-0008](../architecture/adr/0008-strategic-positioning.md).

## 2. vs Lidarr alone

The most common question.

| Concern | Lidarr alone | With Mintarr |
|---|---|---|
| Find new releases | Indexer search | Indexer search (Mintarr aggregates Mintarr-managed sources) |
| Track wanted/missing | Lidarr | Lidarr (unchanged) |
| Fetch from indexers | Download clients | Download clients + Mintarr's source adapters |
| Codec verification before import | Best-effort (Lidarr trusts the filename) | Mandatory (ffprobe codec gate) |
| FLAC integrity check before import | None | `flac -t` per file |
| Detect fake high-resolution (upsampled MP3-into-FLAC) | None | FLAC Detective spectral analysis |
| Block fake imports | Manual (operator inspects after import) | Automatic (V2 BLOCK / REVIEW_REQUIRED) |
| Audit trail for decisions | Lidarr history | Lidarr history + Mintarr sidecars + decisions.jsonl |
| Multi-source unified pipeline | Per-indexer + per-download-client | Single pipeline across TIDAL, LocalFolder, Soulseek, ... |
| Operator review queue for ambiguous imports | None | REVIEW_REQUIRED workflow |

**You want Mintarr if:** you care about FLAC authenticity, multi-source workflows, or a structured review queue.

**Lidarr alone is fine if:** you only fetch from trusted indexers, audio authenticity is not a concern, and you accept Lidarr's import behaviour as-is.

## 3. vs Beets

Beets is a music library manager and tagger. Different problem space.

| Concern | Beets | Mintarr |
|---|---|---|
| Primary role | Tag and organise music library | QC and orchestrate import |
| Tag writing | Yes (core feature) | No (out of scope — ADR-0008) |
| Library reorganisation | Yes | No |
| MusicBrainz lookup | Yes | No |
| AcoustID identification | Yes (plugin) | Future (read-only verifier connector) |
| Spectral analysis | No | Yes (FLAC Detective) |
| Fake-FLAC detection | No | Yes |
| Multi-source ingest | Manual | Source adapter pattern |
| Integration with Lidarr | None (operator runs separately) | Tight (Newznab + SAB) |
| Web UI | Limited (plugin) | Dashboard (Phase 2) |

Beets and Mintarr can co-exist. Run Beets to tag your library; run Mintarr to verify and orchestrate imports. Mintarr's planned beets/Picard verifier connector (F4.5) reads beets output as evidence without writing tags.

**You want both** if you want clean tags AND verified imports.

**You want only Beets** if you don't care about FLAC authenticity and Lidarr's import is good enough.

**You want only Mintarr** if you trust your tags but want pre-import verification.

## 4. vs Picard

Picard is MusicBrainz's official tagger. Same space as Beets but with a different UI philosophy.

| Concern | Picard | Mintarr |
|---|---|---|
| Primary role | Interactive tagging UI | Pipeline-driven QC |
| Workflow | Operator-driven (drag files in, accept matches) | Automated (Lidarr triggers Mintarr) |
| MusicBrainz match | Yes (interactive) | Future read-only verifier |
| Tag writing | Yes | No |
| FLAC verification | Limited | Comprehensive (hard gates + spectral) |

Picard and Mintarr serve different audiences. Picard is for the tag-by-hand audiophile; Mintarr is for the automation-heavy self-hoster.

You can use both. Picard for individual album curation; Mintarr for the high-volume import flow.

## 5. vs soulseek-bridges (slskd, Soularr)

slskd is a Soulseek client. Soularr bridges Lidarr ↔ slskd. Both are upstream of Mintarr.

| Concern | slskd | Soularr | Mintarr |
|---|---|---|---|
| Soulseek peer connection | Yes | No (uses slskd) | No (uses slskd via adapter) |
| Lidarr integration | None | Yes | Yes |
| Pre-import QC | None | None | Yes |
| Fake-FLAC detection on peer-supplied files | None | None | Yes |
| Audit trail | None | Limited | Comprehensive |

Soularr and Mintarr's Soulseek adapter (F3.5, planned) serve similar roles but differ:

- **Soularr:** finds and grabs files via slskd, hands them to Lidarr directly
- **Mintarr Soulseek adapter:** grabs via slskd OR ingests completed-folder, then runs full QC pipeline before Lidarr import

If you trust Soulseek peer quality, Soularr is simpler. If you don't trust peer-supplied files (most operators don't), Mintarr's QC layer catches fake-FLAC before Lidarr imports it.

The two can coexist temporarily, but most operators will pick one path.

## 6. vs custom post-import scripts

Many operators have written post-import scripts that run after Lidarr imports a file — typically to delete or move files based on quality checks.

| Concern | Custom scripts | Mintarr |
|---|---|---|
| When checks run | After Lidarr import | Before Lidarr import |
| Maintenance burden | All on operator | Maintained by Mintarr project |
| Cross-source unification | No (per-script) | Yes (uniform pipeline) |
| Audit trail | Whatever you built | Built in |
| Review queue for ambiguous decisions | No | REVIEW_REQUIRED workflow |
| Dashboard | No | Yes |
| Community contributions | No | Yes (adapter pattern) |

Custom scripts are flexible but accumulate technical debt. Mintarr trades a bit of flexibility (you fit into the four-phase pipeline) for a structured, maintainable system that other people can also benefit from.

**Migrate from custom scripts to Mintarr if:** you've found yourself maintaining the same script for years and it's getting hairy.

**Stay with custom scripts if:** your needs are too specific for Mintarr's pipeline shape (rare).

## 7. vs another arr-stack QC tool

There aren't many. Mintarr's niche is largely uncontested.

| Tool | Status | Compared to Mintarr |
|---|---|---|
| FLAC Detective alone | A spectral analyser; not a pipeline | Mintarr orchestrates FLAC Detective + other verifiers |
| spek + manual | Operator inspects spectra by hand | Mintarr automates the decision and integrates with Lidarr |
| Audiowand | Spectral analyser; commercial | Mintarr is GPL; comparable analysis via FLAC Detective |
| `mediainfo` scripts | Codec detection only | Mintarr does codec detection + integrity + spectral + policy |

If you have a contender that should be in this list, file an issue.

## 8. When Mintarr is the wrong choice

Mintarr is not for everyone. Honest reasons to pass:

- **You don't care about FLAC authenticity.** You trust your sources. Mintarr's value-add is QC; if you don't need QC, the overhead isn't worth it.
- **You don't run Lidarr.** Mintarr's Lidarr coupling is load-bearing in v1. Future OutputConnectors (Plex, Jellyfin direct) relax this; v1 doesn't.
- **You want a single application.** Mintarr is a companion. If you'd rather have one tool instead of Lidarr + Mintarr, Lidarr alone is reasonable.
- **You're not comfortable running Docker containers.** Mintarr ships as a container by default. A bare-metal install path is planned but not v1.
- **You have a very small library.** If you grab one album a month, the value of automated QC is low. Manual inspection is fine.

These are honest. We'd rather have 100 happy operators than 1000 frustrated ones.

## 9. Frequently asked

### "Why isn't Mintarr just a Lidarr plugin?"

Lidarr doesn't support plugins. Adding plugin support to Lidarr is a multi-year upstream effort. Mintarr's Newznab+SAB-bridge approach works against unmodified Lidarr today. See [ADR-0007 §"Alternative 3"](../architecture/adr/0007-no-lidarr-fork.md) for the pre-import-webhook proposal that may eventually replace this.

### "Why is Mintarr in Python when Lidarr is C#?"

Different teams, different language preferences. Mintarr's contributors are Python-native; Lidarr's are C#-native. The Newznab/SAB protocol is language-agnostic, so they coexist fine.

### "Will Mintarr support Lidarr forks?"

If a Lidarr fork uses the same Newznab + SAB API shape, Mintarr should work without modification. Lidarr-NG (if it materialises) would need testing. Mintarr does not officially support forks but does not deliberately break them either.

### "Can Mintarr work with Sonarr or Radarr?"

No. Sonarr and Radarr handle TV and movies; their indexer and download-client semantics are different. Mintarr is music-specific. The four-phase pipeline assumes audio.

---

> Last updated: 2026-05-26
