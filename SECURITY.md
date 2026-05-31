# Security Policy

Mintarr is self-hosted software that touches API keys, OAuth tokens, network endpoints, and the local filesystem. Security issues are taken seriously.

This document covers:

1. Reporting a vulnerability
2. Supported versions
3. Threat model overview
4. What is in scope and what is not

---

## Reporting a vulnerability

**Do not file public GitHub issues for security vulnerabilities.** Public disclosure before a fix gives every Mintarr operator a window of exposure.

### Preferred channel

Use GitHub's [Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) feature on the Mintarr repository. This creates a confidential security advisory only maintainers can see.

### Fallback channel

If GitHub's PVR is unavailable or you prefer email, contact:

- **Eivind Sjursen** — see GitHub profile for current contact methods

Mark the message subject with `[Mintarr Security]` so it routes correctly.

### What to include in the report

- A description of the vulnerability and the attack scenario it enables
- Steps to reproduce, ideally with a proof of concept (PoC)
- The Mintarr version, container image tag, or commit SHA affected
- The Lidarr version Mintarr was configured against (if relevant)
- The deployment shape (Docker Compose, bare metal, behind reverse proxy with SSO, etc.)
- Any suggested remediation, if you have one

We do not require a PoC for the initial report — a credible description is enough to start triage — but a PoC reduces back-and-forth.

### What to expect in response

- **Acknowledgement** within 5 business days
- **Initial triage** (confirmed / not-confirmed / needs-more-info) within 14 business days
- **Status updates** at least every 30 days while the issue is open
- **Fix and coordinated disclosure** when the fix is ready

If maintainers do not respond within these windows, escalate via a follow-up message marked `[Mintarr Security — escalation]`.

### Disclosure timeline

The default is **coordinated disclosure with a 90-day deadline**:

- Day 0: Report received and acknowledged
- Days 1-90: Investigation, fix development, fix testing, release preparation
- Day 90 (or earlier if fix is ready): Coordinated public disclosure

If the vulnerability is being actively exploited in the wild, the timeline compresses. If the fix is more complex than 90 days reasonably allows, maintainers will request a timeline extension from the reporter.

Reporters who follow this process and act in good faith are credited in the security advisory unless they prefer to remain anonymous.

## Supported versions

| Version | Supported |
|---|---|
| `0.x` (foundation phase) | Not supported for security fixes — no public release yet |
| `1.x` (first stable release) | Supported with security fixes during 1.x lifetime |
| Previous major versions | Supported for 6 months after the next major release |

Until Mintarr reaches 1.0, all reports are addressed but security-fix-release cadence is not guaranteed.

## Threat model overview

The full threat model lives in [docs/architecture/SECURITY_MODEL.md](docs/architecture/SECURITY_MODEL.md). The summary:

### Trust boundaries

| Component | Trust level |
|---|---|
| Operator at the keyboard | Trusted |
| API key holder (`X-Api-Key` / `apikey`) | Trusted |
| `Remote-User` from configured reverse proxy | Trusted iff proxy configuration is sound |
| Lidarr (talking to Mintarr via Newznab/SAB API) | Semi-trusted (read its writes carefully) |
| External HTTP services (TIDAL, slskd, flac-detective) | Semi-trusted |
| Source files on disk (LocalFolder) | Semi-trusted (operator-controlled) |
| Soulseek peers | Untrusted (peer-supplied content) |
| Network traffic on container's exposed ports | Untrusted unless proxied behind trusted layer |
| Anyone else | Untrusted |

### Key invariants

These cannot be relaxed without an ADR change:

1. **No source bypasses shared QC before Lidarr import.** Verifier evidence is gathered for every import.
2. **Hard gates cannot be disabled in import mode.** ffprobe and `flac -t` checks are not optional for active import.
3. **Secrets are not stored in browser-visible config.** API keys, OAuth tokens, slskd credentials live in environment variables or Docker secrets.
4. **Docker socket is not mounted into Mintarr by default.** Inspecting Docker state requires network probes, not socket access.
5. **Source files are never modified by Mintarr.** LocalFolder, Soulseek completed-folder, and any future similar source copy files; they do not move or delete.
6. **Path traversal is rejected at multiple layers.** Both endpoint normalisation and adapter-level resolve-and-contain checks reject relative paths that escape configured roots.
7. **Symlinks are rejected outright in source folders.** Symlinked candidate directories or symlinked files within candidate directories are not followed.

### Out-of-scope attack vectors

These are not within Mintarr's security model:

- **Compromised Lidarr.** Mintarr trusts the configured Lidarr instance. If Lidarr is compromised, Mintarr-via-Lidarr is too.
- **Compromised reverse proxy.** Mintarr trusts the `Remote-User` header if a proxy is configured. A compromised proxy can impersonate users.
- **Compromised operator host.** Mintarr cannot defend against a malicious actor with root on the container's host.
- **Network-level attacks against external services.** Mintarr trusts TIDAL, slskd, and flac-detective endpoints to be reachable over operator-trusted network paths.
- **Supply chain attacks against pinned dependencies.** Mintarr pins dependencies and relies on PyPI / GitHub source integrity. A compromised upstream is a broader ecosystem problem.

## What is in scope for a security report

- **Authentication bypass.** Accessing protected endpoints without a valid API key or session.
- **Authorization escalation.** A reader-equivalent caller reaching maintainer-equivalent endpoints (note: Mintarr v1 has no roles — see [ADR-0002](docs/architecture/adr/0002-single-instance-arr-pattern.md) — so this collapses to "unauth bypass").
- **Path traversal.** Reading or writing files outside configured directories via API-provided paths.
- **Subprocess injection.** Crafting input that causes Mintarr to invoke subprocess with attacker-controlled arguments.
- **SSRF.** Inducing Mintarr to make HTTP requests to attacker-chosen URLs.
- **XSS.** Stored or reflected cross-site scripting in the dashboard.
- **CSRF.** Cross-site request forgery against authenticated dashboard endpoints.
- **Secret exposure.** Logs, sidecars, dashboard responses, or audit logs leaking API keys, OAuth tokens, or credentials.
- **Supply-chain pin bypass.** Container build accepting an unpinned or wrong-sha dependency in production.

## What is not in scope for a security report

- **Denial of service via resource consumption** (huge album folders, slow flac-detective HTTP, etc.). Mintarr has caps and timeouts; if a specific limit is missing, that's a feature request, not a vulnerability.
- **Self-DoS via misconfiguration.** Wrong env vars, broken mounts, missing API keys — these produce error responses, not security holes.
- **Best-practice notes without an exploitable scenario.** E.g., "you should use stronger TLS ciphers": file as an issue, not a security report.
- **Bugs in non-Mintarr code.** Lidarr bugs, slskd bugs, ffprobe bugs — report to those projects directly.

## Coordinated disclosure credit

Reporters who follow the process above are credited in the published security advisory as the reporter, unless they request anonymity. We do not currently offer monetary bounties; we are a self-hosted open-source project with no funding model.

## Updates to this policy

This document is versioned with the Mintarr repository. Changes are recorded in commit history.

---

> Last updated: 2026-05-26
