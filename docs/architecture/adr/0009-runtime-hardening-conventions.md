# ADR-0009: Runtime hardening conventions during Mintarr cutover

**Status:** Accepted — locked 2026-05-31
**Deciders:** Eivind Sjursen, Codex
**Related:** [ADR-0001 Rename from tidalhires](0001-rename-from-tidalhires.md), [ADR-0004 API versioning](0004-api-versioning-semver.md), [Security Model](../SECURITY_MODEL.md), [Cutover Manifest](../CUTOVER_MANIFEST.md)

---

## Context

Mintarr is being extracted from the private `tidalhires` subsystem into a public project. That creates a period where the runtime must support both old private names and new public names. It also creates a publication risk: private topology and secrets that were harmless in a private compose stack become public-source regressions if they appear in code, logs or dashboard HTML.

During the 2026-05-31 hardening review, three concrete risks were fixed:

- Unknown routes logged raw request values, which could leak `apikey`, `token` or `password` on typo requests.
- Public docs used `MINTARR_*` env vars, while runtime only accepted `TIDALHIRES_*`, forcing a breaking rename at cutover.
- Dashboard deep-links contained a private Lidarr LAN IP instead of server-injected configuration.

These are not one-off fixes. They are conventions that future changes must preserve.

## Decision

Mintarr runtime changes must follow these hardening conventions:

1. **Unknown and fallback HTTP routes require authentication and redact request values.** If a route logs request args, form values, headers or JSON bodies, it must pass through the shared redaction helper for secret-like keys (`apikey`, `api_key`, `x-api-key`, `password`, `token`, and obvious variants).
2. **Public `MINTARR_*` env vars are introduced as aliases before legacy `TIDALHIRES_*` names are removed.** During cutover, new names take precedence, legacy names remain accepted, and docs state the alias period clearly.
3. **Dashboard/browser-visible config is injected from server configuration, not hardcoded from a private deployment.** URLs, hostnames and ports that appear in HTML/JS must come from env/config or safe localhost/example defaults.

## Consequences

### Positive

- Public cutover can happen incrementally without breaking the private deployment.
- Operators get a predictable migration path from `TIDALHIRES_*` to `MINTARR_*`.
- Typo routes and debug logs do not become accidental secret sinks.
- Public source does not expose private LAN topology.
- PR review has concrete rules rather than subjective "seems safe" judgement.

### Negative

- The runtime carries alias code for at least one release cycle.
- Tests need to cover both legacy and public env-var names.
- Dashboard HTML cannot be treated as a static blob if it needs runtime URLs.

## Required Review Checks

Reviewers must reject a PR if it:

- Adds a new unauthenticated endpoint other than `/health` without a specific security-model update.
- Logs raw request values that may include secrets.
- Replaces an existing legacy env var with a new public name without an alias period.
- Adds browser-visible hardcoded hostnames, private IPs, API keys or local paths.
- Removes the alias behavior before a successor ADR or migration guide explicitly approves the removal.

## Re-evaluation Triggers

This ADR can be superseded only when:

1. The public Mintarr repo has shipped at least one stable release with `MINTARR_*` env vars.
2. The migration guide defines a removal window for legacy `TIDALHIRES_*` aliases.
3. A successor ADR states which aliases or hardening conventions are being relaxed and why.

Until then, these conventions are load-bearing.

---

> Locked: 2026-05-31
