# ADR-0001: Rename from tidalhires to Mintarr

**Status:** Accepted — locked 2026-05-26
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0008 Strategic positioning](0008-strategic-positioning.md)

---

## Context

The project began as `tidalhires` — a single-purpose helper for fetching TIDAL high-resolution audio and importing it to Lidarr. Through F1–F4 design work, the codebase grew to:

- A versioned `SourceAdapter` Protocol with TIDAL, LocalFolder, and Soulseek (designed) implementations
- A multi-source Newznab indexer that aggregates results from any registered adapter
- A planned `Connector` architecture spanning sources, verifiers, and outputs
- A pre-import QC pipeline that is source-agnostic

The name `tidalhires` describes none of this accurately. It implies:

- TIDAL is the only source (false — LocalFolder ships, Soulseek is designed)
- High-resolution audio is the only concern (false — verification, multi-source orchestration, audit trail are core)
- The project is a personal tool (no — the open-source pivot makes it a community project)

For an open-source project aiming at the arr-stack community, the wrong name is the highest-friction onboarding problem we have. A user landing on `tidalhires` will not consider it for their Soulseek + CD-rip + LocalFolder workflow even though the project handles those cases.

## Decision

Rename the project to **Mintarr**.

The rename covers:

- Repository identity (move from `eivindsjursen-lab/sjursen-mediastack` monorepo's `tidalhires/` subdirectory to a new public `eivindsjursen-lab/mintarr` repository)
- Container image name (`tidalhires:local` → `mintarr/mintarr:<version>`)
- Container name in compose files
- Python package, module references in documentation, default config paths
- All operator-facing documentation
- HTTP `User-Agent` and Newznab indexer name advertised to Lidarr

The state database file is renamed `tidalhires_state.db` → `mintarr_state.db`. Existing operators get a migration step in [UPGRADE_GUIDE.md](../../operations/UPGRADE_GUIDE.md).

## Rationale

### "Mintarr" reasoning

- **Arr-namespace.** Matches `Sonarr` / `Lidarr` / `Radarr` / `Prowlarr` / `Readarr` / `Bazarr`. Self-hosting community recognises the pattern instantly.
- **"Mint condition"** is the audiophile term for perfect-quality records and lossless audio. Direct association with what Mintarr does (verifies authenticity).
- **Short, pronounceable, googleable.** Two syllables. No collision with other major open-source projects (verified manually 2026-05-26; should be re-verified before public repo creation).
- **Available on key platforms** (PyPI, GitHub org, Docker Hub) — must be confirmed before final public commit, but no blocker found in initial check.

### Alternatives considered

| Name | Pro | Con | Verdict |
|---|---|---|---|
| `Mintarr` | Arr-family, "mint condition", short, googleable | Could be misread as a cryptocurrency project | **Chosen** |
| `Verifyr` | Descriptive (verify), arr-style | Less catchy, less audiophile-resonant | Rejected — weaker emotional hook |
| `Gauntlet` | Strong metaphor (pipeline as a gauntlet) | Not arr-namespace, common name collision in software | Rejected — discovery problem |
| `Curatr` | "Curator" maps to QC role, arr-style | Less specific to audio | Rejected — too generic |
| `Pristine` | Audiophile resonance | Not arr-namespace, common English word | Rejected — discovery problem |
| Keep `tidalhires` | No migration cost | Misrepresents the project's scope to incoming users | Rejected — defeats the open-source pivot |

The arr-namespace constraint was applied early because it doubles as a built-in audience signal. A self-hoster searching for "Lidarr add-on" or "Lidarr companion" is more likely to find and trust a tool named `Mintarr` than one named `Gauntlet`.

## Consequences

### Positive

- Onboarding friction reduced for the audience Mintarr targets (arr-stack self-hosters)
- The project name no longer falsely implies single-source TIDAL focus
- Container image name aligns with arr-stack convention (`<name>/<name>:tag`)
- Future Lidarr-stack ADRs (custom format conventions, source-tag naming) inherit a coherent brand

### Negative

- Existing private deployments require manual migration:
  - rename `tidalhires_state.db` → `mintarr_state.db`
  - update `docker-compose.yml` service name and image
  - update Lidarr's indexer and download-client URLs if hostnames changed
  - update any external scripts or systemd unit files that reference `tidalhires`
- Git history under the old name remains in the maintainer's private monorepo. The public `mintarr` repo starts with a clean initial commit per [MINTARR_CUTOVER_PLAYBOOK.md](../MINTARR_CUTOVER_PLAYBOOK.md).
- Search-engine ranking starts at zero for the new name. Worth the cost; the old name was never indexed (private repo).

### Accepted trade-offs

- Operators of the legacy `tidalhires` deployment will need to perform a one-time migration. This is documented and is cheaper than carrying the wrong project name forward.
- The Mintarr-on-PyPI / Mintarr-on-Docker-Hub names must be claimed before the first public release. This is an action item, not a blocker for documentation work.

## Migration plan

The migration runs in three phases (matching [MINTARR_DOCUMENTATION_INDEX.md §3](../../MINTARR_DOCUMENTATION_INDEX.md#3-migration-from-tidalhires)):

1. **Documentation phase (current).** Mintarr foundation docs written under existing `docs/` directory with `MINTARR_` prefix or under `docs/strategy/` / `docs/architecture/` subdirectories. Code remains under `tidalhires/`.
2. **Cutover.** New public repo `eivindsjursen-lab/mintarr` created with sanitised history (per audit). Code moved, renamed in place. Old `tidalhires/` directory in monorepo becomes a stub README pointing at the new repo.
3. **Operator migration.** Documented in `UPGRADE_GUIDE.md`. Step-by-step rename of database file, container, image, service name. Backwards-compatible env var names where practical (e.g., `TIDAL_DL_NG_CONFIG` is not renamed).

## Re-evaluation triggers

This decision is durable. It is re-opened only if:

1. **The Mintarr name is unavailable on critical platforms (PyPI / Docker Hub / a Mintarr-claiming entity surfaces).** A successor ADR proposes an alternative.
2. **A naming conflict with another well-known open-source project emerges that makes discovery hostile.** A successor ADR proposes an alternative.

Until then, ADR-0001 stands. Any contribution proposing renaming again must reference one of the above triggers.

---

> Locked: 2026-05-26
