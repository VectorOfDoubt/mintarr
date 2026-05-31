# ADR-0002: Single-instance per container, arr-stack pattern

**Status:** Accepted — locked 2026-05-26
**Deciders:** Eivind Sjursen, Claude
**Related:** [ADR-0008 Strategic positioning](0008-strategic-positioning.md)

---

## Context

When pivoting to open source, the question of how Mintarr handles multiple users became unavoidable. Three patterns exist in the broader self-hosting ecosystem:

1. **Single-instance, single-user.** One database per container, one admin role, optional form login. Multiple users mean multiple containers. Pattern used by Sonarr / Lidarr / Radarr / Prowlarr / Readarr / Bazarr.
2. **Single-instance, multi-user with built-in RBAC.** One database with `users`, `roles`, `permissions` tables and a full identity/access management layer. Pattern used by Nextcloud, Gitea, Vaultwarden.
3. **Multi-tenant.** Per-tenant data isolation through `tenant_id` columns, separate databases, or row-level security. Pattern used by hosted SaaS.

The choice affects the data model, the auth implementation, the UI, and every future feature that touches state. Locking the wrong pattern is expensive to reverse.

## Decision

Mintarr is **single-instance, single-user**, matching the arr-stack pattern.

Concretely:

- One SQLite database per container instance, owned by one admin
- Optional form login with username + password
- API key (`X-Api-Key` header or `apikey` query param) for headless/CLI use
- Reverse-proxy SSO supported via `Remote-User` HTTP header (Authelia / Authentik / Caddy-auth pattern)
- **No roles, no permissions, no per-user data isolation in v1**

Operators who need multiple isolated users run multiple Mintarr containers, each behind its own auth. This is identical to how the arr-stack handles the case (e.g., a family wanting separate Lidarr libraries runs two Lidarr containers).

## Rationale

### Pattern fit

Mintarr's target audience overlaps almost entirely with the existing arr-stack audience. Those users have already accepted the single-instance pattern and have existing infrastructure (Authelia, Authentik, NGINX/Caddy auth-request) for proxy-based SSO when they need multi-user. Adopting their pattern means:

- Operators do not learn new mental models for "how does this app handle users"
- Existing proxy-SSO setups slot in without app-side work
- Mintarr's auth code stays small and reviewable

### Cost asymmetry of alternatives

| Pattern | Implementation cost | Maintenance cost | Audience expectation |
|---|---|---|---|
| Single-instance (chosen) | Small. API key + optional form login + `Remote-User` header support. | Stable. Auth surface rarely changes. | Matches arr-stack expectations. |
| Multi-user with RBAC | Large. User CRUD, role assignment, permission checks on every endpoint, password reset, session management, audit of user actions. | Heavy. Auth bugs are security bugs. Permission boundaries drift as features land. | Not expected for music-import tools. |
| Multi-tenant | Large + invasive. `tenant_id` on every row, query rewriting, per-tenant config, isolation testing. | Heavy. Tenant-leakage bugs are catastrophic. | Not expected for self-hosted tools. |

The marginal value of built-in multi-user is low — the proxy-SSO pattern handles it externally. The cost of building it in is high.

### Multi-user without multi-tenancy

Operators who genuinely want multiple users on the same Mintarr instance have two paths:

1. **Reverse-proxy SSO.** Authelia / Authentik / Keycloak in front of Mintarr. Multiple users see the same single-instance Mintarr; access is gated upstream. Mintarr sees `Remote-User` and logs it for audit.
2. **Multiple instances.** Two Lidarr instances? Two Mintarr instances. Each container is self-contained.

Both paths are documented (Phase 0 docs reference proxy-SSO; UPGRADE_GUIDE shows multi-instance compose patterns). Mintarr does not block either path; it simply does not implement RBAC in v1.

### Future-proofing

If at some point Mintarr genuinely needs multi-user with isolation (e.g., a hosted SaaS or a per-user verifier policy), a successor ADR introduces it. The path forward is:

1. Add `tenant_id` columns to all stateful tables (one-time schema migration)
2. Add user management UI
3. Add RBAC checks on endpoints
4. Document the new model

This is feasible but not free. The triggering conditions are in §Re-evaluation triggers.

## Consequences

### Positive

- Mintarr auth surface stays small (probably <300 LOC including form login + API key + `Remote-User` parsing)
- Documentation can use existing arr-stack proxy-SSO patterns verbatim
- Operators with existing Authelia/Authentik setups onboard Mintarr without auth-config
- New contributors do not need to understand a permission model

### Negative

- Operators with genuine multi-user needs must run multiple Mintarr instances
- Mintarr cannot natively distinguish "Alice's grabs" from "Bob's grabs" on a shared instance — both attribute to the same admin user in audit logs
- Hosted-SaaS deployments are not supported by v1 architecture

### Accepted trade-offs

- "Built-in user management" feature requests will be closed with reference to this ADR and the proxy-SSO pattern
- Multi-instance documentation (`UPGRADE_GUIDE.md`) carries the multi-user story
- Audit log attribution is single-user in v1; if proxy-SSO is in use, the `Remote-User` header is captured but not used for permission decisions

## Alternatives considered

### Alternative 1: Built-in RBAC from v1

Rejected. Cost-benefit unfavourable for the audience Mintarr targets. Auth bugs are security bugs and we would carry the maintenance burden for a feature that the proxy-SSO pattern already handles.

### Alternative 2: API-key only (no form login)

Considered. Simpler than the chosen option. Rejected because the dashboard is the primary operator surface and requiring API-key entry on every browser session is hostile UX. Form login is a small addition (probably <100 LOC) and matches arr-stack expectations.

### Alternative 3: Built-in OIDC (no proxy-SSO option)

Rejected. OIDC client implementation is non-trivial and locks operators into a specific identity provider integration pattern. Reading `Remote-User` from a reverse proxy is simpler, more flexible, and already what the arr-stack ecosystem expects.

## Re-evaluation triggers

This ADR is re-opened only if:

1. **A hosted Mintarr SaaS becomes a project goal.** Single-instance assumes self-host. SaaS requires multi-tenancy and a new architecture.
2. **Mintarr reaches institutional adoption (>100 organisations) and built-in RBAC becomes the dominant feature request.** Justifies the cost.
3. **The arr-stack ecosystem itself adopts built-in multi-user.** Removes the "match the pattern" rationale.

Until then, ADR-0002 stands. Feature proposals for built-in RBAC will be closed with reference to this ADR and the documented proxy-SSO pattern.

---

> Locked: 2026-05-26
