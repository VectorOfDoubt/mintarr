# ADR-0004: SemVer on adapter, connector, and HTTP API contracts

**Status:** Accepted — locked 2026-05-26
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0003 Connector vs Adapter](0003-connector-vs-adapter.md)

---

## Context

Mintarr is intended to support community-contributed adapters. A community-contributed adapter is code outside the Mintarr repo that imports `mintarr.adapters.base` and depends on the shape of the `SourceAdapter` Protocol, the `RawDownload` dataclass, the `PipelineContext` Protocol, and so on.

This creates a binary problem the moment external code exists: any change to those interfaces breaks the community adapter, and there is no mechanism for the community to know *which* change broke it.

The same problem exists for:

- **Connector manifests.** External tools that produce manifest JSON depend on the schema.
- **Sidecar format.** External archival or reporting tools that read `verification.json` depend on the field set.
- **HTTP API surface.** External dashboards, monitoring scripts, or alternative front-ends depend on the endpoint shapes and response schemas.

The default outcome — "we'll be careful" — fails when the project has multiple contributors, AI-assisted PRs, and a multi-year horizon. Carefulness does not scale. A versioning policy does.

## Decision

All public contracts are **SemVer-versioned** through file naming and explicit declaration:

- **Adapter Protocol** — `docs/specs/ADAPTER_PROTOCOL_v1.md`. The `v1` is the major version. Breaking changes produce `_v2.md`; the `_v1.md` file is preserved.
- **Connector manifest** — `docs/specs/CONNECTOR_MANIFEST_v1.md`. Same convention. Manifests declare `api_version: "1.0.0"`.
- **Sidecar format** — `docs/specs/SIDECAR_FORMAT_v2.md`. The current shape is implicitly v2 (V2 verification policy); we document it explicitly with version baked into the filename.
- **HTTP API** — `docs/specs/HTTP_API_v1.md`. Endpoints under `/api/v1/...` for new APIs; existing unversioned endpoints (`/sabnzbd/api`, `/api?t=search`) are external-protocol-compat (SAB, Newznab) and follow those protocols' versioning, not ours.

Each spec file is **immutable once locked**. Editorial fixes (typos, clarifications) can be applied; semantic changes require a successor version.

Mintarr container code declares which version it implements at boot, logged on startup. The dashboard's `/dashboard/v1/connectors` response includes `api_version` per connector. Community adapters declare which version they target in their `pyproject.toml` or similar.

## Rationale

### SemVer is the lowest-friction convention for the audience

The audience writing Mintarr adapters knows SemVer. PyPI uses it, Docker Hub tags use it, GitHub releases use it. Adopting SemVer means adapter authors can reason about compatibility without reading Mintarr-specific documentation.

### File-naming the version makes it cheap to enforce

The version is in the filename. Reviewers see `ADAPTER_PROTOCOL_v1.md` change in a PR and immediately ask "is this an editorial fix or a breaking change requiring v2?". The question is asked because the filename forces it.

If the version were a header inside the file ("Version: 1.2.3"), it would be edited silently and reviewers would have to manually diff old and new to find breakage. The filename convention front-loads the cost of compatibility thinking.

### Multi-version coexistence is realistic

By the time Mintarr reaches v1.0, multiple adapters are likely to exist outside the core repo. Forcing all of them to upgrade simultaneously on every breaking change is hostile. Keeping `_v1.md` next to `_v2.md` lets community adapters stay on v1 until they choose to migrate.

The pipeline supports this by dispatching on the adapter's declared `api_version`. v1 and v2 adapters coexist in the same Mintarr container.

### Editorial vs semantic distinction

Not every change is breaking. A typo fix, a clarification, a new example — these are editorial. A renamed field, a removed method, a tightened constraint — these are semantic and require a new version.

Borderline cases (e.g., "we are clarifying that `priority` was always meant to be 0-100; adapters using 0-200 were always wrong") are decided by maintainer consensus, defaulting to "treat it as breaking". The cost of bumping unnecessarily is low; the cost of breaking external adapters silently is high.

## Consequences

### Positive

- External adapters have a stable contract to pin against
- Mintarr maintainers can iterate the adapter Protocol without paralysing the project around backwards-compat fears (just bump the version)
- Community-published adapters can declare which version they support; users know whether they are compatible without running them
- Documentation reviewers have a clear question to ask on every PR: "is this editorial or semantic?"

### Negative

- The filename convention is unusual (`_v1.md`, `_v2.md`). New contributors may try to "fix" it by consolidating. Documented in CONTRIBUTING.md as deliberate.
- v2 work involves explicit migration documentation. More effort per breaking change than the "we'll be careful" alternative. Worth it.

### Accepted trade-offs

- We will sometimes carry deprecated `_v1.md` files for years before community adapters fully migrate. Disk cost and reader-confusion cost. Mitigated by deprecation notices at the top of superseded files.
- Some refactors that touch the contract will be deferred to coincide with a planned `v2` cutover rather than landed in `v1`, even if they would have been improvements. Acceptable; the priority is contract stability.

## Versioned contracts in scope

- `ADAPTER_PROTOCOL_v{N}.md` — `SourceAdapter`, `VerifierAdapter`, `OutputAdapter` Protocols and supporting dataclasses (`RawDownload`, `ReleaseCandidate`, `PipelineContext`)
- `CONNECTOR_MANIFEST_v{N}.md` — `ConnectorManifest` dataclass shape and runtime-status JSON schema
- `SIDECAR_FORMAT_v{N}.md` — `verification.json` schema
- `HTTP_API_v{N}.md` — Mintarr-native HTTP API (excludes external-protocol endpoints `/sabnzbd/api`, Newznab `/api`, which version per their own protocols)

Not versioned (treated as internal):

- Pipeline phase internals
- state_db schema (operator-side migration tool handles changes)
- Worker queue internals
- Dashboard frontend code

## Alternatives considered

### Alternative 1: Version in document header, not filename

Rejected. Silent edits become possible. Reviewers cannot tell from a PR's file-list that a contract is changing version.

### Alternative 2: No versioning, "we'll be careful"

Rejected. Does not scale beyond a single maintainer, does not scale across AI-assisted PRs, does not survive the multi-year horizon.

### Alternative 3: SemVer the entire repo only, no per-contract versioning

Rejected. Repo-level SemVer is necessary (for release tags) but insufficient. A repo v1.5.0 → v1.5.1 patch can still break an adapter if the adapter contract was not separately versioned. Per-contract versioning catches what repo-level SemVer cannot.

### Alternative 4: Date-based versioning (`_2026.md`, `_2027.md`)

Rejected. Communicates timing but not compatibility. A 2027 version may or may not break 2026 adapters; date does not say.

## Re-evaluation triggers

This ADR is re-opened only if:

1. **The community converges on a different versioning convention that Mintarr would benefit from matching.** (Unlikely but possible.)
2. **The per-contract file-naming convention causes more reviewer confusion than it prevents.** Would need concrete examples; the ADR records the prediction so future re-evaluation has a benchmark.

Until then, ADR-0004 stands. New contracts use the `_v{N}.md` naming. Breaking changes produce successor files; predecessors are preserved.

---

> Locked: 2026-05-26
