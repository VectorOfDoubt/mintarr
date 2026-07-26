# Security Model

> **Type:** Architecture / threat model
> **Version:** 1.0 — 2026-05-26
> **Status:** Living document. Updated as new threats are discovered or new attack surfaces appear.
> **Audience:** Mintarr maintainers. Security researchers. Operators making informed deployment decisions.
> **Related:** [SECURITY.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/SECURITY.md) (operator-facing summary), [OVERVIEW.md](OVERVIEW.md)

---

## 1. Trust boundaries

Mintarr's security model rests on classifying each actor by trust level. Decisions about what to validate, what to log, and what to expose flow from these classifications.

```
┌───────────────────────────────────────────────────────────┐
│ Trusted                                                   │
│   - Operator at the keyboard                              │
│   - API key holder                                        │
│   - Remote-User from configured reverse proxy             │
├───────────────────────────────────────────────────────────┤
│ Semi-trusted (validated input, audit logged)              │
│   - Lidarr instance (via Newznab + SAB APIs)              │
│   - flac-detective HTTP service                           │
│   - TIDAL adapter subprocess and its OAuth flow           │
│   - LocalFolder source files                              │
│   - slskd HTTP service (Phase 4, when implemented)        │
├───────────────────────────────────────────────────────────┤
│ Untrusted (defence in depth)                              │
│   - Soulseek peer-supplied files                          │
│   - Anonymous network traffic on bound ports              │
│   - Stack Overflow code from "fix this for me" PRs        │
└───────────────────────────────────────────────────────────┘
```

The trust levels are conservative. When in doubt, downgrade. Even "trusted" actors get input validation; the difference is which validation failures are logged and which are alerted.

## 2. Authentication

Mintarr v1 has a single trust tier ("admin"). The deployed runtime currently implements API-key authentication only. Reverse-proxy identity and form-login are planned dashboard conveniences, not authorization boundaries.

| Method | Header / param | Strength | Use case |
|---|---|---|---|
| `X-Api-Key` header | `X-Api-Key: <key>` | Strong (constant-time comparison) | Programmatic clients |
| `apikey` query param | `?apikey=<key>` | Strong (constant-time comparison) | Browser-driven flows |
| `Remote-User` header from proxy | `Remote-User: alice` | Planned | Audit attribution via reverse-proxy SSO |
| Form login (planned) | Session cookie | Planned | Dashboard UX |

The API key is set via `MINTARR_API_KEY` after cutover. Current pre-cutover builds also accept `TIDALHIRES_API_KEY`. It must be at least 16 characters; Mintarr refuses to boot with a shorter key. Random 32-character keys are recommended:

```bash
openssl rand -base64 32
```

Form login is a future dashboard feature. It will not replace the API key — both will work in parallel. Sessions will expire after 24 hours by default; configurable via env.

`Remote-User` capture is planned but not implemented in the current runtime. When implemented, it will be captured for audit logging only and will not be used for permission decisions (Mintarr v1 has no roles — [ADR-0002](adr/0002-single-instance-arr-pattern.md)).

## 3. Secrets handling

### 3.1 What counts as a secret

- `MINTARR_API_KEY` (or legacy `TIDALHIRES_API_KEY` before cutover)
- `LIDARR_API_KEY` (or extracted from `LIDARR_CONFIG_XML`)
- TIDAL OAuth tokens in `token.json`
- slskd API key (Phase 4)
- Future source-adapter credentials

### 3.2 Where secrets live

| Secret | Storage | Visible in |
|---|---|---|
| `MINTARR_API_KEY` / legacy `TIDALHIRES_API_KEY` | env var | container env (`docker inspect`) — operator concern |
| `LIDARR_API_KEY` | env var OR extracted from mounted config.xml | env var visible in `docker inspect`; config.xml visible to anyone with read access to the mount |
| TIDAL OAuth | `<TIDAL_DL_NG_CONFIG>/token.json` (mounted from host) | host filesystem at the operator's chosen path |
| slskd API key | env var (planned) | container env |

### 3.3 Where secrets do NOT appear

- **HTTP responses.** No endpoint includes secrets in response bodies. This is tested.
- **Logs.** Mintarr's log redaction strips `apikey` / `password` / `token` from logged request values. Tests verify this for the SAB/Newznab endpoints; the catch-all 404 route also requires auth and redacts request values.
- **Sidecar / decisions.jsonl / state_db.** No column or field carries a secret.
- **Connector status responses.** `GET /dashboard/v1/connectors` returns env var *names* (e.g., `"required_env": ["TIDAL_DL_NG_CONFIG"]`) but never values.
- **Browser-visible config.** F4.3 connector config table stores non-secret settings only.

### 3.4 Constant-time comparison

API key comparisons use `hmac.compare_digest`. Timing-side-channel attacks against the key are not feasible.

### 3.5 Token expiry handling

TIDAL OAuth tokens expire (typically 30 days). Mintarr surfaces this in:

- Dashboard: TIDAL connector shows `health=blocked` with `last_error="token expired"`
- Logs: warning logged when TIDAL session creation fails
- Source-grab attempts: fail with clear error rather than hanging

Operator action: re-run `tidal-dl-ng login` in the relevant config volume; restart Mintarr container or wait for next adapter call to refresh.

## 4. Input validation surfaces

### 4.1 Path traversal

Two ingest paths accept operator-supplied filesystem paths:

- `POST /local/ingest {"path": "..."}` — LocalFolder
- `POST /soulseek/ingest {"path": "..."}` — Soulseek

Both go through the adapter's `normalize_candidate_id`, which:

1. Rejects absolute paths (`path.is_absolute()`)
2. Resolves the path against `<adapter_root>`
3. Rejects if the resolved path escapes `<adapter_root>` (`not resolved.is_relative_to(adapter_root)`)
4. Rejects if the resolved path is not a directory

The same validation is repeated inside `download_raw()` as defence in depth. The endpoint validation happens before job creation; the adapter validation happens at copy time (catches races where the directory disappeared between enqueue and execution).

### 4.2 Symlinks

LocalFolder and Soulseek adapters reject symlinks outright:

- Symlinked candidate directories (the top-level Artist/Album/) — rejected
- Symlinked files within candidate directories — rejected
- Files that resolve outside `<adapter_root>` after symlink-following — rejected (defence in depth)

The rejection happens at copy time. Adapters raise `RuntimeError("symlink blocked: ...")`. The job fails with a clear error message.

### 4.3 base64url encoding

The `/download/<source>/<encoded_source_id>.nzb` endpoint accepts base64url-encoded source IDs to handle paths with spaces, slashes, parentheses.

- Decode errors return HTTP 400 with `"bad encoded source_id"` message
- Decoded source IDs are passed through the adapter's canonicalisation (same as ingest endpoints)
- Path traversal attempts via base64url are caught by the adapter validation

### 4.4 Lidarr response validation

Lidarr is semi-trusted. Mintarr validates response shapes but does not require exact field sets — unexpected fields are ignored. This means Mintarr survives Lidarr API additions without code changes.

Specifically:

- HTTP status codes are checked; non-2xx responses are logged and treated as failures
- JSON parsing failures are caught (not propagated as crashes)
- Required fields (e.g., `album_id` in album-list responses) are extracted with `.get()` and `None`-checked

### 4.5 File content validation

Mintarr does not parse audio file content directly. All audio analysis goes through external tools:

- ffprobe for codec inspection
- flac for integrity check
- flac-detective for spectral analysis

This delegation is deliberate. Parsing audio files in Mintarr's process would create memory-safety risk and dependency surface. External tools run in subprocess with timeouts.

## 5. Subprocess execution

Mintarr invokes external binaries in three places:

1. **TIDAL adapter:** `tidal-dl-ng` for downloads, `tidal-dl-ng cfg` for configuration
2. **Pipeline normalize_audio:** `ffprobe`, `ffmpeg`, `flac -t`
3. **Future adapters:** TBD (Soulseek may invoke slskd-cli; CD-rip lane may invoke cuetools)

All subprocess invocations:

- Use list-of-strings `argv`, not shell strings (no shell injection surface)
- Have an explicit timeout (no indefinite hangs)
- Go through `ctx.run_subprocess` from the adapter context (uniform cancellation)
- Run with the container user's privileges (no setuid)

Subprocess outputs (stdout/stderr) are captured and:

- Logged at INFO (stdout) and WARNING (stderr) levels
- Truncated to last 500 characters in log lines (to avoid log flooding from huge tracebacks)
- Not stored persistently outside the log stream

## 6. Network surface

### 6.1 Inbound

Mintarr's HTTP server listens on a single port (default 8000 inside the container, typically mapped to `127.0.0.1:5025` on the host). Operators are expected to bind to localhost only and access from elsewhere via reverse proxy.

| Endpoint category | Path prefix | Auth required |
|---|---|---|
| Health | `/health` | No (intentional — for monitoring tools) |
| Lidarr-facing | `/api`, `/sabnzbd/api`, `/download/...` | Yes (`apikey` param) |
| Dashboard | `/dashboard`, `/dashboard/v1/...` | Yes (API key; session planned) |
| Ingest | `/local/ingest`, `/soulseek/ingest` | Yes (API key) |

Mintarr does not implement rate limiting (see [HTTP_API_v1.md §12](../specs/HTTP_API_v1.md#12-rate-limits)). Public-facing deployments should rate-limit at the reverse proxy.

### 6.2 Outbound

Mintarr makes outbound HTTP calls to:

- Lidarr API (configurable URL)
- flac-detective HTTP service (configurable URL)
- TIDAL via the tidal-dl-ng subprocess (HTTPS to TIDAL endpoints)
- slskd API (Phase 4)

These should be confirmed to be on the operator-trusted network. Mintarr does not implement TLS-pinning or domain allowlists — operators relying on host-level network policy enforcement should configure that at the container runtime layer.

### 6.3 No Docker socket

Mintarr **does not mount the Docker socket**. Connector health detection (Phase 4.1) uses network probes (`curl` equivalent) to check if external services are reachable, not Docker introspection.

The decision is in [ADR-0003](adr/0003-connector-vs-adapter.md) §"Static-first" and the connector architecture document. Mounting the Docker socket would give Mintarr root-equivalent access to the Docker daemon; the marginal value of "see Docker service status" does not justify the risk.

## 7. Source file integrity

Mintarr's source-file invariants are load-bearing for operator trust:

1. **LocalFolder source files are never modified.** The adapter copies; it does not move. After a successful or failed grab, the source folder is exactly as it was before.
2. **Soulseek source files are never modified.** Same pattern. `.consumed/` markers (if/when introduced) are written to a configurable sibling directory, not into the source.
3. **TIDAL adapter writes to `ctx.raw_dir` only.** It does not modify the operator's TIDAL config beyond updating the `download_base_path` setting (which is per-job and reverts naturally).

These are tested. Mintarr's test suite includes "source UNTOUCHED after grab" assertions for adapters that operate on operator-controlled files.

## 8. Failure modes that are NOT security issues

To prevent over-eager security reports, here are common failure modes that look security-adjacent but are not:

- **Operator misconfigures `MINTARR_API_KEY` to be empty:** Mintarr refuses to boot with a clear error. Not exploitable.
- **Lidarr unreachable:** Pipeline phases continue up to verify; import phase fails with a clear error. Not exploitable.
- **flac-detective unreachable:** V2 fail-closed BLOCKs the import. Operator visible. Not exploitable.
- **Operator's reverse proxy misconfigured to forward `Remote-User` without authentication:** This is a proxy misconfiguration, not a Mintarr vulnerability. Mintarr trusts the proxy by design.
- **Operator commits API key into a public git repo:** Operator's responsibility, not Mintarr's.

For genuinely security-relevant issues, follow [SECURITY.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/SECURITY.md) disclosure process.

## 9. Threat scenarios (high-level)

### 9.1 Attacker on the local network

- **Attempt:** discover Mintarr via port scan, attempt to call endpoints without API key
- **Outcome:** all endpoints except `/health` return 401
- **`/health` exposure:** returns `{"status":"ok"|"degraded","active_jobs":<n>}` only. No sensitive data.

### 9.2 Attacker with stolen API key

- **Attempt:** issue any Mintarr API call
- **Outcome:** full admin access (Mintarr v1 has no roles)
- **Mitigation:** rotate `MINTARR_API_KEY` env var and restart container

### 9.3 Attacker with shell access on the host

- **Outcome:** game over; can read mounted configs, intercept container traffic, modify Mintarr binaries
- **Mintarr cannot defend against this.** Host security is operator's responsibility.

### 9.4 Malicious source files

- **Attempt:** operator (or another local-network actor) drops malicious files into `LOCAL_INGEST_PATH`
- **Mintarr mitigations:** symlink rejection, path traversal rejection, codec gate, file integrity check, flac-detective spectral analysis, V2 BLOCK decisions
- **Limits:** Mintarr cannot detect a steganographically-perfect AAC-in-FLAC that passes ffprobe by accident. The verifier stack catches typical fake-FLAC scenarios.

### 9.5 Compromised Lidarr

- **Outcome:** Mintarr trusts Lidarr's API; a compromised Lidarr can feed false responses to Mintarr
- **Mintarr mitigations:** input validation on response shapes, no privileged actions taken on Lidarr's instruction (Lidarr cannot tell Mintarr to write outside its own directories)

### 9.6 Compromised flac-detective

- **Outcome:** spectral verdict is no longer trustworthy
- **Mintarr mitigations:** hard gates (codec gate, flac -t) still run independently; flac-detective verdict is one component of V2 score, not the only signal; fail-closed if flac-detective is unreachable
- **Limits:** if flac-detective is actively malicious (not just down), it can return ACCEPT for fake files. Defence in depth via multiple verifiers (CTDB, beets/Picard) reduces single-source-of-truth risk.

## 10. Invariants (locked)

These are referenced from [SECURITY.md](https://github.com/eivindsjursen-lab/mintarr/blob/main/SECURITY.md) and [Connector architecture §15](../design/CONNECTOR_PLUGIN_ARCHITECTURE.md). They are load-bearing:

1. **No source bypasses shared QC before Lidarr import.**
2. **Hard gates cannot be disabled in import mode.**
3. **Secrets are not stored in browser-visible config.**
4. **Source files are never modified by Mintarr.**
5. **Path traversal is rejected at multiple layers (endpoint + adapter).**
6. **Symlinks are rejected outright in source folders.**
7. **Docker socket is not mounted into Mintarr by default.**
8. **No source connector is in import mode without required hard-gate verifiers enabled.**
9. **Mintarr never executes operator-supplied code.** No plugin loading from URLs, file uploads, or UI-driven `pip install`.
10. **API keys use constant-time comparison.**

Violating any of these in a PR is grounds for rejection. Changes that affect any of them require an ADR.

## 11. Disclosure and remediation

The public disclosure process, including the best-effort qualification that
applies while the project is paused, is in
[SECURITY.md](https://github.com/VectorOfDoubt/mintarr/blob/main/SECURITY.md).
Summary:

- GitHub Private Vulnerability Reporting preferred
- Email fallback to the maintainer
- 90-day coordinated disclosure default
- Credit in published security advisory unless requested anonymous

For maintainers receiving a report, the response targets below are not a
guaranteed service-level agreement while the project is paused:

1. Acknowledge within 5 business days
2. Triage within 14 business days
3. Coordinate fix development with the reporter
4. Publish security advisory with fix and credit at coordinated disclosure date

## 12. Audit

Mintarr's audit story is reasonable for a small self-hosted application:

- **Actions table** in state_db logs operator actions with timestamp + actor
- **decisions.jsonl** is append-only, capturing every V2 decision
- **Logs** include redacted request lines for HTTP endpoint calls
- **Sidecars** carry per-record lifecycle history

What is NOT audited in v1:

- Authentication events (login, logout, API key rotation) — Phase 0 form-login work
- Connector config changes (F4.3 work adds this)
- Read-only dashboard access — high volume, low value

When in doubt, log. The audit trail is one of Mintarr's value propositions.

---

> Last updated: 2026-05-26
