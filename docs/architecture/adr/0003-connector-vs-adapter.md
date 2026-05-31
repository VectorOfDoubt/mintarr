# ADR-0003: Connector wraps Adapter — two abstractions, one boundary

**Status:** Accepted — locked 2026-05-26
**Deciders:** Codex, Eivind Sjursen, Claude
**Related:** [ADR-0008 Strategic positioning](0008-strategic-positioning.md), [CONNECTOR_PLUGIN_ARCHITECTURE.md](../../design/CONNECTOR_PLUGIN_ARCHITECTURE.md)

---

## Context

By F3.4, Mintarr had a working `SourceAdapter` Protocol: TIDAL and LocalFolder implementations, a registry, dependency-injected `PipelineContext`, four-phase pipeline. The contract was clean and contributors could realistically add new sources without touching common code.

But the contract did not cover the operator-facing questions that mattered for production deployment:

- Is the tool installed in this container?
- Is it enabled?
- Is it healthy?
- What version is running, and is it compatible?
- What configuration does it need?
- What docker service / compose profile installs it?
- Should it appear on the dashboard's Integrations panel?

The natural response was to expand `SourceAdapter` to cover them. That would have produced a 40-method Protocol carrying both code-contract and operator-surface responsibilities. Two failures would follow:

- The code contract becomes unimplementable by simple adapters (50-line Python tools have to implement health-check / version-detection / config-validation)
- The operator surface stays incomplete (some tools have no adapter at all — `ffprobe`, `flac -t`, `flac_detective` are processes, not adapter implementations)

The conflict surfaced when Codex started designing the dashboard's Integrations panel. The data needed for the panel did not match what `SourceAdapter` exposes.

## Decision

Mintarr has **two separate abstractions**:

- **Adapter** — the code contract used by the pipeline. Narrow, simple, focused on "how do files arrive". One per source type, plus future `VerifierAdapter` and `OutputAdapter` variants.
- **Connector** — the operator-facing wrapper. Carries a manifest (`ConnectorManifest`), runtime health, version detection, install hints, and dashboard surface. One per integration, whether or not it has an underlying adapter.

Connectors **wrap** adapters. The wrapping is one-way: connectors call adapter methods; adapters do not know connectors exist. Connectors that have no adapter (a verifier process like `ffprobe`) exist standalone.

The wrapping boundary is documented in [VISION.md](../../strategy/VISION.md), [GLOSSARY.md](../../strategy/GLOSSARY.md), and the connector manifest spec [CONNECTOR_MANIFEST_v1.md](../../specs/CONNECTOR_MANIFEST_v1.md).

## Rationale

### Two questions, two answers

The split aligns the abstractions with the questions being asked.

| Question | Answer |
|---|---|
| "How do files arrive from TIDAL?" | `TidalAdapter.download_raw(ctx)` — Adapter contract |
| "Is TIDAL configured, enabled, and healthy?" | `tidal_connector.health()`, `tidal_connector.manifest.required_env` — Connector contract |
| "How do we run ffprobe on a file?" | `subprocess.run(["ffprobe", ...])` — no adapter; ffprobe has no `download_raw` concept |
| "Is ffprobe installed and what version?" | `ffprobe_connector.health()`, `ffprobe_connector.detected_version` — Connector contract |

Trying to express the second column in a single Protocol with the first column produces noise. Trying to express tools without an adapter (the second row pair) is impossible.

### Contributor cost

A new source adapter is a small contribution — implement four methods (`is_enabled`, `search`, `download_raw`, `cleanup`), write tests, open PR. With Adapter and Connector merged, the same contribution would have required implementing health-check semantics, version detection, install-hint metadata. Half of those are "what should the operator surface say?" questions, not "how do files arrive?" questions.

Separating the abstractions keeps the contributor-facing surface (Adapter) small. The maintainer-facing surface (Connector manifest) is bigger but written once per integration, often by maintainers who can apply project-wide conventions.

### Lifecycle independence

Adapters have a stable contract (`ADAPTER_PROTOCOL_v1.md`). Breaking changes require a `v2` file alongside the `v1` file. The contract evolves on its own schedule, driven by the needs of the pipeline.

Connectors evolve faster, because the dashboard's needs evolve faster than the pipeline's. Phase 2 (sidebar UI), Phase 3 (Prometheus metrics), Phase 4 (new sources) all touch the connector surface. Decoupling them means the adapter contract is not destabilised every time the dashboard adds a field.

### Static-first

The chosen connector model is **static**: connectors are registered in code at boot, not loaded from a plugin directory or installed via UI. This is a deliberate restriction. Dynamic plugin loading carries:

- Code execution from untrusted sources
- Version compatibility between plugin and core
- UI-driven `pip install` operations from a browser session

None of those are reasonable in v1. Static registration gives 80% of the value of a plugin system at 20% of the risk. Dynamic loading is a future option, gated on the static registry being proven stable, and would be its own ADR.

## Consequences

### Positive

- Contributors can author a `SourceAdapter` without touching the connector model
- Maintainers can evolve dashboard / health / version surface (Connector) without destabilising the pipeline contract (Adapter)
- Tools with no adapter (`ffprobe`, `flac -t`, `lidarr_manual_import`) get first-class operator surface through Connector
- The static registry is testable (every connector has a manifest; manifests are checked at boot)

### Negative

- Two abstractions to explain in onboarding. Mitigated by the GLOSSARY definitions and the explicit "Connector wraps Adapter" framing.
- Some integrations need *both* an adapter implementation and a connector manifest. The `TidalConnector` references `TidalAdapter`. Documentation needs to be clear about which file does what.

### Accepted trade-offs

- The connector manifest is verbose (id, kind, display_name, install_profile, required_env, capabilities, docs link, min_supported_version, etc.). The verbosity is the price of useful dashboard surface; it is paid once per integration.
- Dynamic plugin loading is deferred. Operators wanting to install a community-built adapter today get instructions to copy the adapter file into the right location, not a "plugin install" button. The trade-off is worth it for the security position.

## Alternatives considered

### Alternative 1: Single abstraction (Adapter does everything)

Rejected. As described in §Context — produces an unimplementable Protocol for simple adapters and still leaves verifier-only tools without a home.

### Alternative 2: Single abstraction (Connector — no separate Adapter)

Rejected. Connector includes operator-surface metadata that is irrelevant to the pipeline (`docker_service`, `min_supported_version`, `capabilities` strings). Forcing the pipeline to depend on metadata makes the pipeline harder to test in isolation.

### Alternative 3: Three abstractions (Adapter, Manifest, RuntimeStatus)

Considered. Would separate static manifest data from dynamic runtime status. Rejected for v1 because the conceptual overhead is too high for the gain — Connector cleanly carries both, and runtime status can live as Connector instance state.

### Alternative 4: Dynamic plugin loading from v1

Rejected for v1, deferred indefinitely. The security and version-management costs are too high while the contract is still evolving. Re-opens when the static contract has been stable for 12+ months and external demand is concrete.

## Re-evaluation triggers

This ADR is re-opened only if:

1. **An adapter implementation realistically needs to participate in connector-level concerns** (e.g., an adapter that knows its own version and reports it to the connector layer in a way that cannot be modelled as Connector reading from Adapter). Indicates the boundary is wrong.
2. **External demand for dynamic plugin loading becomes concrete** — multiple credible contributors asking for it, with realistic use cases that the static model cannot serve. Would justify the v2 plugin-loader ADR.

Until then, ADR-0003 stands. Connector and Adapter remain two abstractions, with Connector wrapping Adapter.

---

> Locked: 2026-05-26
