# Mintarr — AI agent handover

> **Type:** Onboarding document for AI agents (Claude, Codex, others) joining Mintarr.
> **Version:** 2.0 — 2026-05-26 (rewritten from `tidalhires/AGENT_HANDOVER.md`)
> **Status:** Living document — updated as Mintarr's invariants evolve.
> **Audience:** AI agents about to contribute to Mintarr. Read this **before** making changes.

---

## Read this first

If you are an AI agent (Claude, Codex, or otherwise) and you are about to make a change to Mintarr, read this document in full before touching any file. It contains the load-bearing context that the rest of the docs assume.

The most important rule: **Mintarr has hard scope boundaries.** Most reasonable-sounding feature proposals are out of scope and will be rejected on PR review. The boundary is documented in [ADR-0008 Strategic positioning](../architecture/adr/0008-strategic-positioning.md) and applied via the boundary test in [CONTRIBUTING.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/CONTRIBUTING.md). Reading those two before opening a PR saves wasted work.

## What Mintarr is, in one sentence

> Mintarr is the quality control and import orchestration layer that Lidarr lacks. It is not a better Lidarr.

That sentence is locked in [ADR-0008](../architecture/adr/0008-strategic-positioning.md) and reproduced verbatim in [README.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/README.md), [VISION.md](../strategy/VISION.md), and (eventually) [COMPARISON.md](../community/COMPARISON.md). If you are about to write something that contradicts it, stop and re-read those documents.

## Where you are in the codebase

Mintarr is a Python 3.12 Flask application. Code is under `app/`, tests under `tests/`, documentation under `docs/`.

Key files for orientation:

- `app/server.py` — Flask routes and lifecycle (Newznab + SAB API + dashboard + ingest endpoints)
- `app/pipeline.py` — the 4-phase common pipeline (`download_raw` → `normalize_audio` → `verify` → `import_to_lidarr`)
- `app/verification.py` — V2 policy (the rules that turn evidence into decisions)
- `app/state_db.py` — SQLite state index (query layer over sidecars)
- `app/worker.py` — SQLite-backed worker queue (N=1 by default)
- `app/adapters/` — source adapters (`tidal.py`, `local_folder.py`, future `soulseek.py`, `sab.py`, `qbit.py`)
- `app/dashboard.py` — dashboard backend (Flask blueprint)

For where each subsystem fits, read [docs/architecture/OVERVIEW.md](../architecture/OVERVIEW.md). For pipeline phase invariants, read [docs/architecture/PIPELINE.md](../architecture/PIPELINE.md).

## How Mintarr's contracts are versioned

Mintarr commits to four versioned contracts:

| Contract | Spec file | Used by |
|---|---|---|
| `SourceAdapter` Protocol | `docs/specs/ADAPTER_PROTOCOL_v1.md` | Community adapter authors |
| `ConnectorManifest` | `docs/specs/CONNECTOR_MANIFEST_v1.md` | Connector code, dashboard, install hints |
| Sidecar (`verification.json`) format | `docs/specs/SIDECAR_FORMAT_v2.md` | Anyone reading sidecars (operators, dashboards, archival) |
| HTTP API | `docs/specs/HTTP_API_v1.md` | External tools talking to Mintarr |

The `_vN.md` in the filename is the major version. Breaking changes produce a successor file (`_v2.md`); the predecessor is preserved. The rationale and convention are in [ADR-0004](../architecture/adr/0004-api-versioning-semver.md).

Editorial changes (typos, clarifications) can be applied to locked specs. Semantic changes require a new file. If you are not sure which side a change is on, ask in the PR description.

## Hard invariants (do not violate without an ADR)

These survive any feature work. Violating them in a PR is grounds for rejection.

1. **No source bypasses shared QC.** Every import goes through `normalize_audio` → `verify` → `import_to_lidarr`. Adapters own only `download_raw`.
2. **Hard gates cannot be disabled in import mode.** `ffprobe` codec check and `flac -t` integrity check are required.
3. **Source files are never modified.** Adapters copy; they do not move or delete source files. LocalFolder and Soulseek source-folders remain untouched after a successful or failed grab.
4. **Sidecars are the source of truth.** state_db is a query index rebuilt from sidecars. Do not introduce state in state_db that does not also exist in sidecars.
5. **Secrets are not browser-visible.** API keys, OAuth tokens, slskd credentials live in environment variables or Docker secrets. They never appear in dashboard responses, connector config endpoints, or audit logs.
6. **Path traversal is rejected at multiple layers.** Endpoints normalise and validate; adapters re-validate at copy time. Symlinks are rejected outright in source folders.
7. **Docker socket is not mounted into Mintarr.** Connector health detection uses network probes, not Docker introspection.
8. **No source connector is in import mode without hard gates enabled.** The connector manifest tracks this; the dashboard surfaces violations.
9. **Adapters do not import from `server.py` or `pipeline.py`.** Dependency direction is one-way: `server.py` builds a `PipelineContext` and passes it; the adapter uses what the context exposes and nothing more.
10. **Tests are not optional.** Every PR includes tests for new logic. Existing tests must remain green. CI enforces this; PR review enforces it too.

The full locked-invariant list is in [Connector architecture §15](../design/CONNECTOR_PLUGIN_ARCHITECTURE.md). The list above is the high-frequency subset.

## Anti-patterns observed in past contributions

These have all happened before. If you find yourself doing one of them, stop and reconsider.

### "Let me just add MusicBrainz integration"

Mintarr does not own the MusicBrainz model. That is Lidarr's territory. The boundary test in [ADR-0008](../architecture/adr/0008-strategic-positioning.md) catches this; the PR will be closed with a reference. If the use case requires MusicBrainz data, the right path is either (a) read it from Lidarr's API, or (b) propose an upstream PR to Lidarr.

### "Let me bypass the boundary test by adding the feature incrementally"

ADR-0008 explicitly forbids this. "Adding the feature incrementally" means landing scope-creep without an ADR change. If the boundary is wrong for a specific case, propose a successor ADR. Do not work around the locked boundary.

### "I'll write the feature first and add tests later"

Tests are not optional. The PR review will hold the change until tests exist. Writing tests after the fact is harder than writing them along with the code.

### "Let me skip the documentation update — the code is the documentation"

Documentation is a deliverable. PRs that change behaviour without updating documentation are held in review. The documentation set is mapped in [MINTARR_DOCUMENTATION_INDEX.md](../MINTARR_DOCUMENTATION_INDEX.md) — find the right document for your change and update it in the same commit.

### "Let me skip the design doc — it's a small change"

Small changes do not need design docs. Non-trivial changes do. The rule of thumb: if you are touching multiple files across `app/`, the pipeline, or the state model, write a design doc under `docs/design/F<number>_<name>.md` first. Codex and Claude have followed this pattern for F3.1 through F3.5 and it has saved enormous rework.

### "Let me change the adapter Protocol without bumping the version"

The adapter Protocol is `docs/specs/ADAPTER_PROTOCOL_v1.md`. Breaking changes require `_v2.md`. Silent edits are rejected. See [ADR-0004](../architecture/adr/0004-api-versioning-semver.md).

### "Let me add a global mutable state"

Mintarr's worker queue is N=1 specifically because the codebase has some global state that does not survive concurrency. Adding more global state makes future parallelism harder. If you need shared state, route it through `state_db` (persistent) or the `_jobs` dict (in-memory, lock-protected). Do not add new module-level mutables.

### "I'll just import server.py from the adapter"

No. Adapters depend only on `app/adapters/base.py` and `app/adapters/context.py`. The dependency is one-way. If you find yourself needing something from `server.py`, the right answer is to add it to the `PipelineContext` Protocol, not to import from `server.py`.

## What is currently in flight

This section is volatile. The agent reading this should also check [`MINTARR_DOCUMENTATION_INDEX.md`](../MINTARR_DOCUMENTATION_INDEX.md) and [`ROADMAP.md`](../strategy/ROADMAP.md) for the live status.

As of 2026-05-31:

- **Phase 0 (open-source foundation)** is complete. The cutover playbook ran and the public Mintarr repo exists.
- **License is locked: AGPL-3.0-only** ([ADR-0005](../architecture/adr/0005-license.md)). LICENSE file at repo root.
- **v0.1.0 is shipped** from the public repo. Post-cutover review landed in public commit `5398687` and private staging sync commit `e814383`.
- **Public repo is the source of active Mintarr work.** Work in WSL at `/home/esj006/projects/mintarr`; the old private monorepo is legacy/staging reference, not the primary implementation workspace.
- **F4.1 Static connector registry** is implemented in `app/connectors/` with `GET /dashboard/v1/connectors`. The design is in [F4.1_STATIC_CONNECTOR_REGISTRY.md](../design/F4.1_STATIC_CONNECTOR_REGISTRY.md) and the broader architecture is in [CONNECTOR_PLUGIN_ARCHITECTURE.md](../design/CONNECTOR_PLUGIN_ARCHITECTURE.md).
- **F4.2 Integrations dashboard** is implemented on top of F4.1. It adds a server-rendered Integrations tab that consumes `/dashboard/v1/connectors`; config mutation remains deferred to F4.3.
- **F3.5a Soulseek completed-folder ingest** should come after F4.2/F4.3 so it registers through the connector model rather than becoming another standalone adapter to migrate later.
- **Legacy `tidalhires` Docker runtime may still be live.** Do not stop or delete it during feature work. It currently maps `127.0.0.1:5025->8000` from image `tidalhires:local`, and Lidarr may still point at it as indexer/download client. Cut over only after a Mintarr container is deployed, Lidarr indexer + download client are repointed or verified, API keys match, health is green, and 2-5 dogfood imports succeed. Keep token/config/state/sidecar backups before removing the old runtime.
- **v0.2.0 cleanup issues #9-#15** exist in the public GitHub repo. They cover mypy, ruff format, ruff per-file ignores, legacy design-doc migration, operator docs, frontend framework evaluation, and performance baseline.

If you are picking up work, the next units in priority order are:

1. Add **F4.3 connector config / dry-run** for enable/disable and mode persistence
2. Implement **F3.5a Soulseek completed-folder ingest** through the connector registry
3. Work down **v0.2.0 cleanup #9-#15** as parallel/small PRs
4. Begin Phase 2 dashboard redesign only after ADR-0011 resolves the frontend framework question

Suggested F4.3 branch:

```bash
cd /home/esj006/projects/mintarr
git checkout main
git pull origin main
git checkout -b feat/f4-connector-config
```

Then read:

- Draft `docs/design/F4.3_CONNECTOR_CONFIG_DRY_RUN.md` before implementation if it does not exist yet
- [F4.2_INTEGRATIONS_DASHBOARD.md](../design/F4.2_INTEGRATIONS_DASHBOARD.md)
- [F4.1_STATIC_CONNECTOR_REGISTRY.md](../design/F4.1_STATIC_CONNECTOR_REGISTRY.md)
- [CONNECTOR_PLUGIN_ARCHITECTURE.md](../design/CONNECTOR_PLUGIN_ARCHITECTURE.md)
- [CONNECTOR_MANIFEST_v1.md](../specs/CONNECTOR_MANIFEST_v1.md)
- [HTTP_API_v1.md](../specs/HTTP_API_v1.md) dashboard connector endpoint section

F4.1 runtime surface now available:

- `app/connectors/` with `ConnectorManifest`, `ConnectorHealth`, `Connector` Protocol/base, registry helpers, and concrete connectors for existing source/verifier/output integrations
- `GET /dashboard/v1/connectors` endpoint with auth
- Tests for manifest shape, duplicate registration, required connector subset, runtime status JSON, and endpoint auth/shape
- `CONNECTOR_MANIFEST_v1` is runtime-backed by the implementation

F4.2 runtime surface now available:

- Dashboard tab controls for Records / Integrations
- Integrations view grouped by source, verifier, and output connectors
- Connector runtime status cards showing health, installed/enabled/mode, versions, env names, docker hints, docs links, and last errors
- Existing stack meta line uses connector health from `/dashboard/v1/connectors` when present

## Decisions you should not re-litigate

These have been considered, decided, and locked in ADRs. Do not propose reversing them without a successor ADR.

| Decision | ADR | Why locked |
|---|---|---|
| No Lidarr fork | [0007](../architecture/adr/0007-no-lidarr-fork.md) | Cost asymmetry, scope discipline |
| Companion (not standalone), positioning | [0008](../architecture/adr/0008-strategic-positioning.md) | Defensible market position |
| Rename to Mintarr | [0001](../architecture/adr/0001-rename-from-tidalhires.md) | Open-source audience signal |
| Single-instance per container | [0002](../architecture/adr/0002-single-instance-arr-pattern.md) | arr-stack pattern match, RBAC cost |
| Connector wraps Adapter | [0003](../architecture/adr/0003-connector-vs-adapter.md) | Code contract vs operator surface |
| SemVer on contracts | [0004](../architecture/adr/0004-api-versioning-semver.md) | Community adapter stability |
| AGPL-3.0-only license | [0005](../architecture/adr/0005-license.md) | Combined work includes AGPL TIDAL dependency; AGPL §13 applies to network-hosted deployments |
| MkDocs Material docs | [0006](../architecture/adr/0006-docs-tooling-mkdocs-material.md) | Audience fit, build cost |

If a proposed change conflicts with any of these, raise the conflict in the PR description and link the relevant ADR. Maintainers will then decide whether the conflict is real (ADR re-evaluation triggered) or whether the proposal needs to be reshaped.

## How to coordinate with other agents

Mintarr has had multiple AI agents (Claude, Codex) contributing in alternation. When you arrive, check:

- Recent commits on `main` — what has the other agent landed?
- Open PRs — what is in review?
- [`MINTARR_DOCUMENTATION_INDEX.md`](../MINTARR_DOCUMENTATION_INDEX.md) §6 "Document tracker" — who owns the first draft of each document?
- Recent design docs under `docs/design/` — what is being planned?

If the other agent claims ownership of a document (via the "Owner" column in the index), wait or rebase. Two agents touching the same file produces merge pain.

When you take ownership, update the index in the same commit so the other agent sees the claim. Claude-vs-Codex coordination has worked well via this convention.

## How to know when to stop

The Mintarr maintainers (currently Eivind, with Claude and Codex assisting) value scope discipline highly. If you are working on something and it feels like the change is growing, stop and check:

- Is this still within the boundary test from [ADR-0008](../architecture/adr/0008-strategic-positioning.md)?
- Is this still within the design doc, or has it drifted?
- Is the PR going to be reviewable, or will it be a 2000-line "did everything" commit?

Big PRs are rejected even if every line is technically correct. The right unit of work is a single design doc's worth — typically 200-800 lines of change, including tests and docs.

If you find yourself writing 2000+ lines for a single PR, split it. The other agents and the human maintainer will thank you.

## How to record what you did

Every Mintarr commit follows conventional-commits format ([COMMIT_CONVENTION.md](COMMIT_CONVENTION.md)). For non-trivial work, the commit message body should record:

- What was done (one sentence)
- Why it was done (the design doc reference)
- What is being deferred (any related work that did not fit this PR)
- Any new ADR, design doc, or spec version this commit introduces

Codex and Claude have both followed this convention; the result is that `git log --oneline` is itself a roadmap of how Mintarr evolved.

## When in doubt

- Read the linked document
- Check the relevant ADR
- Open an issue describing what you're stuck on
- Do not silently make a decision that should be discussed

Mintarr is built without a deadline ([VISION.md](../strategy/VISION.md)). It is always better to wait for clarity than to land a change that has to be reverted.

---

> Last updated: 2026-05-26
